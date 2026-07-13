"""reCAPTCHA v3 token minting strategies for the bezkolejki watcher.

Every /Slot/* API call on uw.bezkolejki.eu requires a fresh reCAPTCHA v3
token. Two providers are supported, dispatched by config.captcha_provider
(see resolve_captcha_provider):

- BrowserCaptchaProvider: mints in-page via grecaptcha.execute. This is the
  reliable method for this specific site (confirmed via extensive live
  testing). Tokens minted back-to-back from one page degrade in score until
  the server starts rejecting calls with HTTP 400, so mints are spaced apart
  by CAPTCHA_MINT_SPACING_SECONDS -- do not remove or shrink that spacing.
- TwoCaptchaProvider: solves via the 2Captcha HTTP API instead. Live-tested
  against uw.bezkolejki.eu and confirmed the server REJECTS 2Captcha-solved
  tokens even at min_score=0.9 -- this is a currently-unused-but-still
  supported, config-gated code path, kept for if the site's requirements
  ever change. Not dead code to delete.

Both providers still require the Playwright page passed into mint(): it is
not optional even in "2captcha" mode. The token itself is validated
server-side by Google against the site key + action regardless of which
browser (or no browser) minted it, but the page is still needed by the
caller for the auth Bearer token (stored in localStorage), Cloudflare
clearance, and the in-page fetch that actually submits the API call.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Any

from constants import (
    CAPTCHA_MINT_SPACING_SECONDS,
    RECAPTCHA_SITE_KEY,
    RESERVATION_PAGE_URL,
    TWOCAPTCHA_POLL_INTERVAL_SECONDS,
    TWOCAPTCHA_RESULT_URL,
    TWOCAPTCHA_SUBMIT_URL,
    TWOCAPTCHA_TIMEOUT_SECONDS,
)
from errors import GrabPipelineError

logger = logging.getLogger("bezkolejki_bot")


def _http_get_json(url: str, timeout: float = 30.0) -> dict:
    """Blocking GET that parses a JSON response. Meant to be run via
    loop.run_in_executor so it doesn't block the asyncio event loop. Used
    only for the 2Captcha HTTP API (submit + poll)."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


class CaptchaProvider:
    """Interface: mint(page, action) -> token."""

    async def mint(self, page: Any, action: str) -> str:
        raise NotImplementedError


class BrowserCaptchaProvider(CaptchaProvider):
    """Mints a fresh reCAPTCHA v3 token in-page via grecaptcha.execute.

    Mints are throttled to at least CAPTCHA_MINT_SPACING_SECONDS apart,
    tracked via `_last_mint` -- this instance's own state (this was
    `self._last_captcha_mint` on BezkolejkiClient before the split; a
    provider instance now owns its own mint-spacing clock).
    """

    def __init__(self) -> None:
        self._last_mint: float = 0.0

    async def mint(self, page: Any, action: str) -> str:
        elapsed = time.monotonic() - self._last_mint
        if elapsed < CAPTCHA_MINT_SPACING_SECONDS:
            await asyncio.sleep(CAPTCHA_MINT_SPACING_SECONDS - elapsed)
        token = await page.evaluate(
            """
            async (args) => {
                const [key, action] = args;
                await new Promise(r => grecaptcha.ready(() => r()));
                return await grecaptcha.execute(key, {action});
            }
            """,
            [RECAPTCHA_SITE_KEY, action],
        )
        self._last_mint = time.monotonic()
        return token


class TwoCaptchaProvider(CaptchaProvider):
    """Solves reCAPTCHA v3 via the 2Captcha HTTP API: submits the job, then
    polls for the result. Uses urllib.request in a thread executor so it
    doesn't block the asyncio event loop (no new pip dependency).
    """

    def __init__(self, api_key: str, min_score: float) -> None:
        self._api_key = api_key
        self._min_score = min_score

    async def mint(self, page: Any, action: str) -> str:
        # `page` is unused for the mint itself here (Google validates the
        # token server-side against the site key + action regardless of
        # which browser -- or no browser -- minted it) but is still part of
        # the shared interface: callers rely on the same page for the auth
        # token / Cloudflare clearance / in-page fetch used elsewhere.
        if not self._api_key:
            raise GrabPipelineError("2captcha provider selected but TWOCAPTCHA_API_KEY is empty")

        loop = asyncio.get_running_loop()
        started = time.monotonic()

        submit_params = {
            "key": self._api_key,
            "method": "userrecaptcha",
            "version": "v3",
            "googlekey": RECAPTCHA_SITE_KEY,
            "pageurl": RESERVATION_PAGE_URL,
            "action": action,
            "min_score": str(self._min_score),
            "json": "1",
        }
        logger.info("2captcha: submitting reCAPTCHA v3 job for action=%s", action)
        submit_url = TWOCAPTCHA_SUBMIT_URL + "?" + urllib.parse.urlencode(submit_params)
        submit_result = await loop.run_in_executor(None, _http_get_json, submit_url)

        if submit_result.get("status") != 1:
            raise GrabPipelineError(f"2captcha submit failed: {submit_result.get('request')}")
        captcha_id = submit_result["request"]

        poll_params = {
            "key": self._api_key,
            "action": "get",
            "id": captcha_id,
            "json": "1",
        }
        poll_url = TWOCAPTCHA_RESULT_URL + "?" + urllib.parse.urlencode(poll_params)

        while True:
            elapsed = time.monotonic() - started
            if elapsed > TWOCAPTCHA_TIMEOUT_SECONDS:
                raise TimeoutError(
                    f"2captcha: timed out after {elapsed:.0f}s waiting for job {captcha_id}"
                )
            await asyncio.sleep(TWOCAPTCHA_POLL_INTERVAL_SECONDS)
            poll_result = await loop.run_in_executor(None, _http_get_json, poll_url)
            if poll_result.get("status") == 1:
                token = poll_result["request"]
                logger.info(
                    "2captcha: solved job %s for action=%s in %.1fs",
                    captcha_id, action, time.monotonic() - started,
                )
                return token
            if poll_result.get("request") == "CAPCHA_NOT_READY":
                continue
            raise GrabPipelineError(f"2captcha poll failed: {poll_result.get('request')}")


def resolve_captcha_provider(config: Any) -> CaptchaProvider:
    """Construct the effective CaptchaProvider for `config`.

    Mirrors the provider-resolution logic that used to live in
    BezkolejkiClient.__init__ exactly: "auto" resolves to "2captcha" if
    config.twocaptcha_api_key is set, else "browser"; any other explicit
    value in config.captcha_provider ("browser" or "2captcha") is used as-is.
    """
    if config.captcha_provider == "auto":
        effective = "2captcha" if config.twocaptcha_api_key else "browser"
    else:
        effective = config.captcha_provider

    if effective == "2captcha":
        return TwoCaptchaProvider(config.twocaptcha_api_key, config.captcha_min_score)
    return BrowserCaptchaProvider()
