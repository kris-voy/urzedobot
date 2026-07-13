"""BezkolejkiClient - browser management + API calls (all via page.evaluate)."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

from captcha import resolve_captcha_provider
from config import Config
from constants import (
    BASE_URL,
    CAPTCHA_MAX_RETRIES,
    CAPTCHA_RETRY_DELAY_SECONDS,
    COMPANY_NAME,
    RECAPTCHA_SITE_KEY,
    RESERVATION_PAGE_URL,
    USER_AGENT,
)
from errors import GrabPipelineError, RateLimitedError

logger = logging.getLogger("bezkolejki_bot")


class BezkolejkiClient:
    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        # Resolve the effective captcha provider once at construction time:
        # "auto" becomes "2captcha" if an API key is configured, else "browser".
        # (This mirrors the "auto" resolution captcha.resolve_captcha_provider
        # does internally — duplicated here only for the human-readable name
        # used in logs/status, so callers don't need to reach into the
        # provider object's internals just to know which one got picked.)
        if self.config.captcha_provider == "auto":
            self._captcha_provider = "2captcha" if self.config.twocaptcha_api_key else "browser"
        else:
            self._captcha_provider = self.config.captcha_provider
        self._captcha = resolve_captcha_provider(self.config)

    async def start(self):
        logger.info(
            "Starting Playwright browser... (captcha provider: %s | headless: %s)",
            self._captcha_provider, self.config.headless,
        )
        self._playwright = await async_playwright().start()
        launch_args = ["--disable-blink-features=AutomationControlled"]
        if os.name != "nt":
            launch_args.insert(0, "--no-sandbox")
        proxy = None
        if self.config.proxy_server:
            proxy = {"server": self.config.proxy_server}
            if self.config.proxy_username:
                proxy["username"] = self.config.proxy_username
                proxy["password"] = self.config.proxy_password
            logger.info("Using proxy: %s", self.config.proxy_server)
        self._browser = await self._playwright.chromium.launch(
            headless=self.config.headless,
            args=launch_args,
            proxy=proxy,
        )
        await self._open_fresh_page()
        logger.info("Browser ready, reCAPTCHA injected, page loaded.")

    async def _open_fresh_page(self):
        """Create a brand-new browser context + page and load the site. Each
        fresh context resets the reCAPTCHA v3 session, which is essential:
        minting many tokens from one long-lived page makes the score decay and
        the server starts rejecting them (observed: 1st cycle OK, then all fail).
        Called at start and refreshed before each polling cycle."""
        # Close the previous context (if any) so we don't leak contexts/pages.
        try:
            if self._context is not None:
                await self._context.close()
        except Exception:
            pass
        self._context = await self._browser.new_context(
            user_agent=USER_AGENT,
            locale="pl-PL",
        )
        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        self._page = await self._context.new_page()
        await self._page.goto(RESERVATION_PAGE_URL, wait_until="networkidle")
        await self._inject_recaptcha()

    async def refresh_page(self):
        """Public: start a fresh reCAPTCHA session for the next cycle."""
        await self._open_fresh_page()

    async def stop(self):
        logger.info("Stopping browser...")
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    async def restart(self):
        await self.stop()
        await self.start()

    async def _inject_recaptcha(self):
        assert self._page is not None
        await self._page.evaluate(
            """
            (key) => new Promise((res, rej) => {
                if (window.grecaptcha && window.grecaptcha.execute) return res();
                const s = document.createElement('script');
                s.src = 'https://www.google.com/recaptcha/api.js?render=' + key;
                s.onload = res; s.onerror = rej;
                document.head.appendChild(s);
            })
            """,
            RECAPTCHA_SITE_KEY,
        )
        # wait for grecaptcha.execute to become available
        await self._page.wait_for_function(
            "() => window.grecaptcha && window.grecaptcha.execute"
        )

    async def _mint_captcha_token(self, action: str) -> str:
        assert self._page is not None
        return await self._captcha.mint(self._page, action)

    async def _get_stored_token(self) -> Optional[str]:
        assert self._page is not None
        return await self._page.evaluate("() => localStorage.getItem('token')")

    async def _set_stored_token(self, token: str):
        assert self._page is not None
        await self._page.evaluate("(t) => localStorage.setItem('token', t)", token)

    async def ensure_auth_token(self):
        """Fetch a fresh auth token if none is stored yet."""
        token = await self._get_stored_token()
        if token:
            return token
        token = await self._fetch_new_token()
        await self._set_stored_token(token)
        return token

    async def _fetch_new_token(self) -> str:
        assert self._page is not None
        result = await self._page.evaluate(
            """
            async (baseUrl) => {
                const resp = await fetch(baseUrl + '/Authentication/GetEmptyToken/ouw', {method: 'GET'});
                const status = resp.status;
                // Read the body ONCE (a Response stream can't be read twice),
                // then try to parse it as JSON.
                const raw = await resp.text();
                let body = null;
                try { body = JSON.parse(raw); } catch (e) { body = null; }
                return {status, body, raw};
            }
            """,
            BASE_URL,
        )
        if result["status"] != 200 or not isinstance(result["body"], dict) or "token" not in result["body"]:
            snippet = (result.get("raw") or "")[:300]
            raise GrabPipelineError(
                f"Failed to get auth token (HTTP {result.get('status')}): {snippet!r}"
            )
        return result["body"]["token"]

    async def _api_call(
        self,
        method: str,
        path: str,
        action: str,
        query_params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        include_h_captcha: bool = False,
        captcha_retries_left: int = CAPTCHA_MAX_RETRIES,
        captcha_body_key: Optional[str] = None,
    ) -> dict:
        """
        Perform an authenticated + recaptcha-protected API call via page.evaluate,
        using in-page fetch. Handles the one-retry-after-5s-on-400 rule (minting a
        FRESH captcha token on the retry, never reusing a stale one) and raises
        RateLimitedError on 429.

        The captcha token is placed as a query param (?recaptchaToken=...) when
        query_params is not None, OR - if captcha_body_key is given - as a key
        inside json_body instead (used for endpoints like BlockSlot/
        ConfirmReservation whose documented body shape embeds "captchaToken").
        """
        assert self._page is not None
        token = await self.ensure_auth_token()
        captcha_token = await self._mint_captcha_token(action)

        params = None
        if query_params is not None:
            params = dict(query_params)
            params["recaptchaToken"] = captcha_token
            if include_h_captcha:
                # Intentional: the site's own SPA passes the literal "fakeToken"
                # for hCaptchaToken when hCaptcha-before-calendar is disabled
                # (which it is for these queues). Verified in the site JS bundle.
                params["hCaptchaToken"] = "fakeToken"

        body = None
        if json_body is not None:
            body = dict(json_body)
            if captcha_body_key:
                body[captcha_body_key] = captcha_token

        result = await self._page.evaluate(
            """
            async (args) => {
                const {baseUrl, path, method, token, params, body} = args;
                let url = baseUrl + path;
                if (params) {
                    const qs = new URLSearchParams(params).toString();
                    url += (url.includes('?') ? '&' : '?') + qs;
                }
                const opts = {
                    method,
                    headers: {
                        'Authorization': 'Bearer ' + token,
                    },
                };
                if (body !== null && body !== undefined) {
                    opts.headers['Content-Type'] = 'application/json';
                    opts.body = JSON.stringify(body);
                }
                const resp = await fetch(url, opts);
                const status = resp.status;
                let respBody = null;
                try { respBody = await resp.json(); } catch (e) {
                    try { respBody = await resp.text(); } catch (e2) { respBody = null; }
                }
                return {status, body: respBody};
            }
            """,
            {
                "baseUrl": BASE_URL,
                "path": path,
                "method": method,
                "token": token,
                "params": params,
                "body": body,
            },
        )

        status = result["status"]
        resp_body = result["body"]

        if status == 429:
            raise RateLimitedError(f"429 on {path}: {resp_body}")

        if status == 400 and captcha_retries_left > 0:
            attempt = CAPTCHA_MAX_RETRIES - captcha_retries_left + 1
            backoff = CAPTCHA_RETRY_DELAY_SECONDS * attempt  # 5s, then 10s
            logger.warning(
                "%s -> HTTP 400 (likely captcha validation), retry %d/%d after %ss: %s",
                path, attempt, CAPTCHA_MAX_RETRIES, backoff, resp_body,
            )
            await asyncio.sleep(backoff)
            return await self._api_call(
                method, path, action, query_params, json_body, include_h_captcha,
                captcha_retries_left=captcha_retries_left - 1,
                captcha_body_key=captcha_body_key,
            )

        if status >= 400:
            raise GrabPipelineError(f"{path} -> HTTP {status}: {resp_body}")

        # if the response carries a fresh token, replace the stored one
        if isinstance(resp_body, dict) and resp_body.get("token"):
            await self._set_stored_token(resp_body["token"])

        return {"status": status, "body": resp_body}

    # -- Public API methods ---------------------------------------------------

    async def get_available_days(self, operation_id: int) -> dict:
        res = await self._api_call(
            "GET",
            "/Slot/GetAvailableDaysForOperation",
            "GetAvailableDaysForOperation",
            query_params={"companyName": COMPANY_NAME, "lastStepId": operation_id},
        )
        return res["body"]

    async def get_available_slots(self, operation_id: int, day: str) -> list:
        res = await self._api_call(
            "GET",
            "/Slot/GetAvailableSlotsForOperationAndDay",
            "GetAvailableSlotsForOperationAndDay",
            query_params={"companyName": COMPANY_NAME, "lastStepId": operation_id, "day": day},
            include_h_captcha=True,
        )
        body = res["body"]
        return body if isinstance(body, list) else body.get("slots", []) if isinstance(body, dict) else []

    async def block_slot(self, slot_id) -> dict:
        res = await self._api_call(
            "POST",
            "/Slot/BlockSlot",
            "BlockSlot",
            query_params=None,
            json_body={
                "slotId": slot_id,
                "companyName": COMPANY_NAME,
                "captchaToken": "",  # placeholder, injected fresh by _api_call
                "captchaV2Token": "",
                "blockedBy": 1,
            },
            captcha_body_key="captchaToken",
        )
        return res["body"]

    async def get_properties_for_slot(self, slot_id) -> Any:
        """
        NOTE: this endpoint's exact parameter shape was NOT live-verified.
        We pass companyName + slotId + recaptchaToken (matching the pattern of
        every other Slot endpoint) and log the full response so the real shape
        can be confirmed / adjusted on first real use.
        """
        try:
            res = await self._api_call(
                "GET",
                "/Slot/GetPropertiesForSlot",
                "GetPropertiesForSlot",
                query_params={"companyName": COMPANY_NAME, "slotId": slot_id},
            )
            logger.info("GetPropertiesForSlot response: %s", res["body"])
            return res["body"]
        except GrabPipelineError as e:
            logger.error("GetPropertiesForSlot failed (unverified endpoint, see spec notes): %s", e)
            raise

    async def update_slot_properties(self, properties: list) -> dict:
        res = await self._api_call(
            "POST",
            "/Slot/UpdateSlotProperties",
            "UpdateSlotProperties",
            query_params={},  # recaptchaToken appended by _api_call via query_params
            json_body={"isAnonymous": False, "properties": properties},
        )
        return res["body"]

    async def confirm_reservation(self) -> dict:
        res = await self._api_call(
            "POST",
            "/Slot/ConfirmReservation",
            "ConfirmReservation",
            query_params=None,
            json_body={
                "captchaToken": "",  # placeholder, injected fresh by _api_call
                "isAnonymous": False,
                "smsCode": None,
                "allowSendDocument": True,
            },
            captcha_body_key="captchaToken",
        )
        return res["body"]

    @property
    def is_alive(self) -> bool:
        # Check the browser connection too, not just the Page flag: a crashed
        # Chromium / dropped CDP connection leaves the Page object "open" but
        # every evaluate() fails — detecting it here triggers a proactive
        # restart instead of burning 3 failed cycles first.
        if self._browser is not None and not self._browser.is_connected():
            return False
        return self._page is not None and not self._page.is_closed()
