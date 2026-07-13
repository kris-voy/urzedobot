"""
bezkolejki.eu (uw.bezkolejki.eu/ouw) appointment-slot watcher + Telegram notifier.

Watches three reservation queues on the Polish government "bezkolejki" booking
site for free appointment slots. When a slot appears it can (depending on
MODE) just notify, block the slot, or block + fill the reservation form and
wait for a Telegram button press to confirm.

Run on Windows with:  python3.14 bot.py
See README.md for setup instructions.

CLI flags:
    --once          run a single check cycle, print results, exit
    --dry-run       never call BlockSlot/ConfirmReservation (acts as notify_only)
    --test-telegram send a test Telegram message and exit
    --test-slot     send a SIMULATED 'slot found' alert (see what a real one looks like)
    --healthcheck   end-to-end reliability check (site + captcha + telegram), exit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Any, Optional

from dotenv import load_dotenv

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py<3.9 fallback, not expected here
    ZoneInfo = None  # type: ignore

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
from telegram.error import TelegramError


# Windows consoles default to cp1252 and choke on emoji / Polish chars in our
# log + status output. Force UTF-8 on the streams (no-op where already UTF-8).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


# =============================================================================
# Config
# =============================================================================

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(_BASE_DIR, ".env")
load_dotenv(ENV_PATH)
# Personal data for auto-fill lives in a separate file so operational config
# (.env) stays free of one person's private details. Loaded second; its FORM_*
# keys override any leftover FORM_* in .env. Optional — absent file is fine.
APPLICANT_ENV_PATH = os.path.join(_BASE_DIR, "applicant.env")
load_dotenv(APPLICANT_ENV_PATH, override=True)

WARSAW_TZ = ZoneInfo("Europe/Warsaw") if ZoneInfo else None

COMPANY_NAME = "ouw"
BASE_URL = "https://uw.bezkolejki.eu/api"
RESERVATION_PAGE_URL = "https://uw.bezkolejki.eu/ouw/Reservation"
RECAPTCHA_SITE_KEY = "6LeCXbUUAAAAALp9bXMEorp7ONUX1cB1LwKoXeUY"

QUEUE_OPERATION_IDS = {
    "A": 3213864,
    "B": 3219596,
    "C": 3219597,
}

USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
).strip() or (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

CONFIRM_BUTTON_TIMEOUT_SECONDS = 4 * 60
POST_SUCCESS_KEEPALIVE_SECONDS = 10 * 60
MAX_CONSECUTIVE_FAILURES = 3
RATE_LIMIT_BACKOFF_SECONDS = 5 * 60
CAPTCHA_RETRY_DELAY_SECONDS = 5
# Minimum gap between consecutive reCAPTCHA v3 token mints. Minting several
# tokens back-to-back from the same headless page yields low scores that the
# server rejects with HTTP 400 ("Error while verify captcha"), so we space them
# out generously — reliability matters far more than shaving seconds here.
CAPTCHA_MINT_SPACING_SECONDS = 3.0
# How many times to re-mint + retry a call that 400s on captcha validation
# (in addition to the first attempt), with escalating backoff.
CAPTCHA_MAX_RETRIES = 2
NOTIFY_DEDUPE_SECONDS = 30 * 60

# --- 2Captcha (optional captcha-solving-service provider) --------------------
TWOCAPTCHA_SUBMIT_URL = "https://2captcha.com/in.php"
TWOCAPTCHA_RESULT_URL = "https://2captcha.com/res.php"
TWOCAPTCHA_POLL_INTERVAL_SECONDS = 5.0
TWOCAPTCHA_TIMEOUT_SECONDS = 120.0


def _env(name: str, default: str = "") -> str:
    val = os.environ.get(name)
    return val if val is not None else default


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return default
    try:
        return float(val)
    except ValueError:
        return default


@dataclass
class Config:
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    queues: list = field(default_factory=lambda: [
        q.strip().upper() for q in _env("QUEUES", "A,B,C").split(",") if q.strip()
    ])
    mode: str = field(default_factory=lambda: _env("MODE", "block_and_fill").strip().lower())
    auto_confirm: bool = field(default_factory=lambda: _env_bool("AUTO_CONFIRM", False))
    fast_window: str = field(default_factory=lambda: _env("FAST_WINDOW", "05:45-08:45"))
    fast_interval_seconds: int = field(default_factory=lambda: _env_int("FAST_INTERVAL_SECONDS", 30))
    slow_interval_seconds: int = field(default_factory=lambda: _env_int("SLOW_INTERVAL_SECONDS", 300))
    jitter_percent: float = field(default_factory=lambda: _env_float("JITTER_PERCENT", 0.35))
    # Fresh reCAPTCHA session granularity. False (default): one fresh page per
    # cycle (all queues share it) — lighter footprint, fine on a healthy IP that
    # can do ~3 mints per page. True: a fresh page per QUEUE (every mint is a
    # "first mint") — most robust on a marginal IP, but ~3x the page loads.
    fresh_page_per_queue: bool = field(default_factory=lambda: _env_bool("FRESH_PAGE_PER_QUEUE", False))
    form_data: dict = field(default_factory=dict)
    captcha_provider: str = field(default_factory=lambda: _env("CAPTCHA_PROVIDER", "auto").strip().lower())
    twocaptcha_api_key: str = field(default_factory=lambda: _env("TWOCAPTCHA_API_KEY", "").strip())
    captcha_min_score: float = field(default_factory=lambda: _env_float("CAPTCHA_MIN_SCORE", 0.3))
    headless: bool = field(default_factory=lambda: _env_bool("HEADLESS", True))
    # Reliability / observability
    startup_notify: bool = field(default_factory=lambda: _env_bool("STARTUP_NOTIFY", True))
    stale_alert_minutes: int = field(default_factory=lambda: _env_int("STALE_ALERT_MINUTES", 15))
    heartbeat_hours: float = field(default_factory=lambda: _env_float("HEARTBEAT_HOURS", 0.0))
    # Optional (residential) proxy — needed on datacenter/VPS IPs that Cloudflare
    # + reCAPTCHA block. e.g. PROXY_SERVER=http://gate.example.com:8000
    proxy_server: str = field(default_factory=lambda: _env("PROXY_SERVER", "").strip())
    proxy_username: str = field(default_factory=lambda: _env("PROXY_USERNAME", "").strip())
    proxy_password: str = field(default_factory=lambda: _env("PROXY_PASSWORD", "").strip())

    def __post_init__(self):
        if not self.form_data:
            for key, val in os.environ.items():
                if key.startswith("FORM_") and val.strip():
                    label = key[len("FORM_"):].replace("_", " ").strip()
                    self.form_data[label] = val.strip()
        if not self.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is not set — the bot cannot notify you.")
        if not str(self.telegram_chat_id).strip():
            raise ValueError("TELEGRAM_CHAT_ID is not set — notifications would be silently dropped.")
        if not self.queues:
            raise ValueError("QUEUES is empty — nothing to watch. Set e.g. QUEUES=A,B,C")
        invalid = [q for q in self.queues if q not in QUEUE_OPERATION_IDS]
        if invalid:
            raise ValueError(f"Invalid queue letters in QUEUES: {invalid}. Valid: A, B, C")
        if self.mode not in ("notify_only", "block", "block_and_fill"):
            raise ValueError(f"Invalid MODE: {self.mode!r}")
        if self.captcha_provider not in ("auto", "browser", "2captcha"):
            raise ValueError(f"Invalid CAPTCHA_PROVIDER: {self.captcha_provider!r}. Valid: auto, browser, 2captcha")


def parse_fast_window(window_str: str) -> tuple:
    """Parse 'HH:MM-HH:MM' into (start_time, end_time)."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$", window_str)
    if not m:
        raise ValueError(f"Invalid FAST_WINDOW format: {window_str!r} (expected HH:MM-HH:MM)")
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    return dtime(h1, m1), dtime(h2, m2)


def is_within_fast_window(window_str: str) -> bool:
    start, end = parse_fast_window(window_str)
    now = datetime.now(WARSAW_TZ).time() if WARSAW_TZ else datetime.now().time()
    if start <= end:
        return start <= now <= end
    # window wraps past midnight
    return now >= start or now <= end


def jittered(seconds: float, pct: float = 0.35) -> float:
    """Randomize an interval by +/- pct so the polling cadence isn't a fixed,
    bot-like tick. Each sleep is independently random within [1-pct, 1+pct]."""
    delta = seconds * pct
    return max(1.0, seconds + random.uniform(-delta, delta))


def strip_diacritics(s: str) -> str:
    normalized = unicodedata.normalize("NFKD", s)
    return "".join(c for c in normalized if not unicodedata.combining(c))


def normalize_label(s: str) -> str:
    return strip_diacritics(s).lower().strip()


def fuzzy_match_field(field_label: str, form_data: dict) -> Optional[str]:
    """Match a form field's label against configured FORM_* keys.

    Exact (normalized) match wins. Otherwise fall back to substring matching in
    either direction, but pick the MOST SPECIFIC (longest normalized key) among
    all candidates — several keys share the token "numer " (numer sprawy /
    dokumentu / paszportu), and picking arbitrarily by dict order could submit
    the wrong ID number to a government form. Ambiguity is logged loudly."""
    norm_label = normalize_label(field_label)
    # exact match first
    for key, value in form_data.items():
        if normalize_label(key) == norm_label:
            return value
    # substring match either direction — collect all, prefer the longest key
    candidates = []
    for key, value in form_data.items():
        nk = normalize_label(key)
        if nk and (nk in norm_label or norm_label in nk):
            candidates.append((nk, key, value))
    if not candidates:
        return None
    candidates.sort(key=lambda c: len(c[0]), reverse=True)
    if len(candidates) > 1:
        logger.warning(
            "Field %r fuzzy-matched %d config keys %s — using most specific (%r). "
            "Verify this is correct before confirming!",
            field_label, len(candidates), [c[1] for c in candidates], candidates[0][1],
        )
    return candidates[0][2]


def _http_get_json(url: str, timeout: float = 30.0) -> dict:
    """Blocking GET that parses a JSON response. Meant to be run via
    loop.run_in_executor so it doesn't block the asyncio event loop. Used
    only for the 2Captcha HTTP API (submit + poll)."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


# =============================================================================
# Logging
# =============================================================================

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bezkolejki_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)


class GrabPipelineError(Exception):
    """Raised when a step in the block/fill/confirm pipeline fails."""


class RateLimitedError(Exception):
    """Raised on HTTP 429."""


# =============================================================================
# BezkolejkiClient - browser management + API calls (all via page.evaluate)
# =============================================================================

class BezkolejkiClient:
    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._last_captcha_mint: float = 0.0
        # Resolve the effective captcha provider once at construction time:
        # "auto" becomes "2captcha" if an API key is configured, else "browser".
        if self.config.captcha_provider == "auto":
            self._captcha_provider = "2captcha" if self.config.twocaptcha_api_key else "browser"
        else:
            self._captcha_provider = self.config.captcha_provider

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
        """
        Mint a fresh reCAPTCHA v3 token for the given action, dispatching to
        the effective provider (see __init__): "2captcha" solves it via the
        2Captcha HTTP API (higher scores, avoids the automated-browser
        penalty); "browser" mints it in-page via grecaptcha.execute (the
        original method, spaced out to avoid tanking the score).

        Note: even in "2captcha" mode the Playwright page is still required -
        it's not optional. The token itself is validated server-side by
        Google against the site key + action regardless of which browser (or
        no browser) minted it, but this bot still needs the page for the
        auth Bearer token (stored in localStorage), Cloudflare clearance, and
        the in-page fetch that actually submits the API call in _api_call.
        """
        assert self._page is not None
        if self._captcha_provider == "2captcha":
            return await self._mint_captcha_token_2captcha(action)

        # browser method: throttle mint spacing (irrelevant for 2captcha,
        # which is already slow and whose scoring doesn't depend on it).
        elapsed = time.monotonic() - self._last_captcha_mint
        if elapsed < CAPTCHA_MINT_SPACING_SECONDS:
            await asyncio.sleep(CAPTCHA_MINT_SPACING_SECONDS - elapsed)
        token = await self._page.evaluate(
            """
            async (args) => {
                const [key, action] = args;
                await new Promise(r => grecaptcha.ready(() => r()));
                return await grecaptcha.execute(key, {action});
            }
            """,
            [RECAPTCHA_SITE_KEY, action],
        )
        self._last_captcha_mint = time.monotonic()
        return token

    async def _mint_captcha_token_2captcha(self, action: str) -> str:
        """
        Solve reCAPTCHA v3 via the 2Captcha HTTP API. Submits the job, then
        polls for the result. Uses urllib.request in a thread executor so it
        doesn't block the asyncio event loop (no new pip dependency).
        """
        api_key = self.config.twocaptcha_api_key
        if not api_key:
            raise GrabPipelineError("2captcha provider selected but TWOCAPTCHA_API_KEY is empty")

        loop = asyncio.get_event_loop()
        started = time.monotonic()

        submit_params = {
            "key": api_key,
            "method": "userrecaptcha",
            "version": "v3",
            "googlekey": RECAPTCHA_SITE_KEY,
            "pageurl": RESERVATION_PAGE_URL,
            "action": action,
            "min_score": str(self.config.captcha_min_score),
            "json": "1",
        }
        logger.info("2captcha: submitting reCAPTCHA v3 job for action=%s", action)
        submit_url = TWOCAPTCHA_SUBMIT_URL + "?" + urllib.parse.urlencode(submit_params)
        submit_result = await loop.run_in_executor(None, _http_get_json, submit_url)

        if submit_result.get("status") != 1:
            raise GrabPipelineError(f"2captcha submit failed: {submit_result.get('request')}")
        captcha_id = submit_result["request"]

        poll_params_base = {
            "key": api_key,
            "action": "get",
            "id": captcha_id,
            "json": "1",
        }
        poll_url = TWOCAPTCHA_RESULT_URL + "?" + urllib.parse.urlencode(poll_params_base)

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


# =============================================================================
# Fill helper: match config form data to dynamic field definitions
# =============================================================================

def build_filled_properties(field_defs: Any, form_data: dict) -> list:
    """
    field_defs: whatever GetPropertiesForSlot returned (list of field definition
    dicts, expected to have at least a "name"/"label" and a value slot). Since
    the exact shape is unverified, this is defensive: it looks for common key
    names and fills what it can, leaving the rest of the structure untouched
    so UpdateSlotProperties gets a shape as close as possible to what the site
    expects.
    """
    if not isinstance(field_defs, list):
        logger.warning("GetPropertiesForSlot did not return a list (got %s); cannot auto-fill.",
                        type(field_defs))
        return []

    filled = []
    for f in field_defs:
        if not isinstance(f, dict):
            filled.append(f)
            continue
        label = f.get("name") or f.get("label") or f.get("propertyName") or f.get("displayName") or ""
        match = fuzzy_match_field(str(label), form_data) if label else None
        new_field = dict(f)
        if match is not None:
            # Only set value-keys that ACTUALLY exist on the field; invent a
            # plain "value" only if none of the known value-keys were present.
            # (Previously this always injected a spurious "value" key, risking a
            # payload shape the API rejects.)
            present = [k for k in ("value", "propertyValue", "fieldValue") if k in new_field]
            if present:
                for k in present:
                    new_field[k] = match
            else:
                new_field["value"] = match
            logger.info("Matched form field %r -> %r", label, match)
        filled.append(new_field)
    return filled


# =============================================================================
# Telegram notifier
# =============================================================================

class TelegramNotifier:
    def __init__(self, config: Config):
        self.config = config
        self.app: Optional[Application] = None
        self.paused = False
        self.last_check_summary = "never"
        self.last_check_time: Optional[datetime] = None
        self.watcher_ref = None  # set by Watcher so /status can show reliability stats
        # single-flight confirm/release state
        self._pending_confirm: Optional[asyncio.Future] = None
        self._pending_lock = asyncio.Lock()

    async def start(self):
        if not self.config.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
        self.app = Application.builder().token(self.config.telegram_bot_token).build()
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("pause", self._cmd_pause))
        self.app.add_handler(CommandHandler("resume", self._cmd_resume))
        self.app.add_handler(CommandHandler("test", self._cmd_test))
        self.app.add_handler(CallbackQueryHandler(self._on_button))
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot polling started.")

    async def stop(self):
        if self.app:
            try:
                await self.app.updater.stop()
            except Exception:
                pass
            try:
                await self.app.stop()
            except Exception:
                pass
            try:
                await self.app.shutdown()
            except Exception:
                pass

    def _is_authorized(self, update: Update) -> bool:
        chat = update.effective_chat
        return chat is not None and str(chat.id) == str(self.config.telegram_chat_id)

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        state = "PAUSED" if self.paused else "running"
        last = self.last_check_time.strftime("%Y-%m-%d %H:%M:%S %Z") if self.last_check_time else "never"
        lines = [
            f"Status: {state}",
            f"Last check: {last}",
            f"Last result: {self.last_check_summary}",
        ]
        w = self.watcher_ref
        if w is not None:
            now = datetime.now(WARSAW_TZ) if WARSAW_TZ else datetime.now()
            health = "⚠️ STALE (can't check)" if w.stale_alerted else "✅ healthy"
            lines += [
                f"Health: {health}",
                f"Uptime: {w._fmt_duration(now - w.started_at)}",
                f"Checks OK: {w.total_successful_checks}/{w.total_checks}",
                f"Slot hits: {w.total_slots_found}",
                f"Mode: {w.effective_mode} | captcha: {w.client._captcha_provider}",
            ]
        await update.message.reply_text("\n".join(lines))

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        self.paused = True
        await update.message.reply_text("Paused. Polling will skip cycles until /resume.")

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        self.paused = False
        await update.message.reply_text("Resumed.")

    async def _cmd_test(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        await update.message.reply_text("Test OK - bot is alive and listening.")

    async def _on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if query is None or not self._is_authorized(update):
            if query:
                await query.answer()
            return
        await query.answer()
        data = query.data
        async with self._pending_lock:
            fut = self._pending_confirm
        if fut is not None and not fut.done():
            if data == "confirm":
                fut.set_result("confirm")
                await query.edit_message_reply_markup(reply_markup=None)
            elif data == "release":
                fut.set_result("release")
                await query.edit_message_reply_markup(reply_markup=None)
        else:
            await query.edit_message_reply_markup(reply_markup=None)

    async def send(self, text: str, reply_markup=None) -> bool:
        """Returns True if the message was delivered, False otherwise."""
        if not self.app:
            logger.warning("Telegram app not started; cannot send: %s", text)
            return False
        try:
            await self.app.bot.send_message(
                chat_id=self.config.telegram_chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=False,
            )
            return True
        except Exception as e:  # TelegramError and anything else — never raise
            logger.error("Failed to send Telegram message: %s", e)
            return False

    async def ask_confirm_or_release(self, summary_text: str) -> str:
        """
        Send a message with Confirm/Release buttons and wait (up to
        CONFIRM_BUTTON_TIMEOUT_SECONDS) for a button press. Returns
        'confirm', 'release', 'timeout', or 'send_failed' (prompt never
        delivered — caller should not silently burn the full timeout).
        """
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Potwierdz", callback_data="confirm"),
                InlineKeyboardButton("❌ Zwolnij", callback_data="release"),
            ]
        ])
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        async with self._pending_lock:
            self._pending_confirm = fut
        if not await self.send(summary_text, reply_markup=keyboard):
            async with self._pending_lock:
                self._pending_confirm = None
            logger.error("Confirm prompt failed to send — not waiting for a button.")
            return "send_failed"
        try:
            result = await asyncio.wait_for(fut, timeout=CONFIRM_BUTTON_TIMEOUT_SECONDS)
            return result
        except asyncio.TimeoutError:
            return "timeout"
        finally:
            async with self._pending_lock:
                self._pending_confirm = None


# =============================================================================
# Dedup tracker for notify_only mode
# =============================================================================

class DedupeTracker:
    def __init__(self, window_seconds: int):
        self.window_seconds = window_seconds
        self._seen: dict = {}

    def should_notify(self, queue: str, day: str) -> bool:
        key = (queue, day)
        now = time.monotonic()
        last = self._seen.get(key)
        if last is None or (now - last) > self.window_seconds:
            self._seen[key] = now
            return True
        return False


# =============================================================================
# Main watcher
# =============================================================================

class Watcher:
    def __init__(self, config: Config, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.client = BezkolejkiClient(config)
        self.notifier = TelegramNotifier(config)
        self.dedupe = DedupeTracker(NOTIFY_DEDUPE_SECONDS)
        self.consecutive_failures = 0
        self.stopped = False
        self.effective_mode = "notify_only" if dry_run else config.mode
        # Reliability tracking
        now = datetime.now(WARSAW_TZ) if WARSAW_TZ else datetime.now()
        self.started_at = now
        self.last_success_time = now
        self.stale_alerted = False
        self._stale_since = now
        self.last_heartbeat_time = now
        self.total_checks = 0
        self.total_successful_checks = 0
        self.total_slots_found = 0
        self.notifier.watcher_ref = self  # let /status read live reliability stats

    @staticmethod
    def _fmt_duration(delta) -> str:
        secs = int(abs(delta.total_seconds()))
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h{m}m"
        if m:
            return f"{m}m{s}s"
        return f"{s}s"

    async def check_all_queues_once(self) -> dict:
        """Runs one check cycle across all configured queues. Returns a dict
        of queue -> availableDays (or 'ERROR')."""
        results = {}
        # Shuffle order each cycle so no queue is systematically favored/starved.
        order = list(self.config.queues)
        random.shuffle(order)
        for i, queue in enumerate(order):
            # Refresh the reCAPTCHA session (fresh context). ALWAYS before the
            # first queue (reusing a page across cycles fully breaks the score);
            # before every queue too if FRESH_PAGE_PER_QUEUE (each mint a "first
            # mint" — most robust on a marginal IP, at ~3x the page loads).
            try:
                if i == 0 or self.config.fresh_page_per_queue:
                    await self.client.refresh_page()
                op_id = QUEUE_OPERATION_IDS[queue]
                data = await self.client.get_available_days(op_id)
                days = data.get("availableDays", []) if isinstance(data, dict) else []
                results[queue] = days
            except RateLimitedError:
                raise
            except Exception as e:
                logger.error("Error checking queue %s: %s", queue, e)
                results[queue] = "ERROR"
        return results

    async def handle_available_queue(self, queue: str, days: list):
        op_id = QUEUE_OPERATION_IDS[queue]
        earliest_day = sorted(days)[0]
        logger.info("Queue %s has availability on %s - fetching slots.", queue, earliest_day)

        # Fresh context for the slot-fetch/grab (the loop left the page on the
        # last queue's context). In block modes the ensuing block→confirm mints
        # must share ONE context (the hold is tied to that session), so starting
        # it fresh gives those mints their best captcha score.
        try:
            await self.client.refresh_page()
        except Exception as e:
            logger.error("refresh before handling queue %s failed: %s", queue, e)

        try:
            slots = await self.client.get_available_slots(op_id, earliest_day)
        except Exception as e:
            logger.error("Failed to fetch slots for queue %s day %s: %s", queue, earliest_day, e)
            await self.notifier.send(
                f"Slot detected for queue {queue} on {earliest_day} but fetching slot "
                f"times FAILED ({e}).\nGo check manually NOW: {RESERVATION_PAGE_URL}"
            )
            return

        if not slots:
            logger.warning("Queue %s reported available day %s but no slots returned.", queue, earliest_day)
            return

        self.total_slots_found += 1

        earliest_slot = slots[0]
        slot_id = earliest_slot.get("id") if isinstance(earliest_slot, dict) else earliest_slot
        slot_time = earliest_slot.get("time") if isinstance(earliest_slot, dict) else None
        times_preview = ", ".join(
            str(s.get("time", s.get("id"))) if isinstance(s, dict) else str(s)
            for s in slots[:10]
        )

        if self.effective_mode == "notify_only":
            if not self.dedupe.should_notify(queue, earliest_day):
                logger.info("Skipping duplicate notification for queue %s / %s (deduped).", queue, earliest_day)
                return
            await self.notifier.send(
                f"<b>Free slot found!</b>\nQueue: {queue}\nDay: {earliest_day}\n"
                f"Times: {times_preview}\n{RESERVATION_PAGE_URL}"
            )
            return

        # block / block_and_fill: single-flight, act immediately
        try:
            await self.notifier.send(
                f"Slot found on queue {queue}, day {earliest_day} "
                f"(time {slot_time or slot_id}). Attempting to BLOCK it now..."
            )
            block_result = await self.client.block_slot(slot_id)
            logger.info("BlockSlot result for queue %s: %s", queue, block_result)
        except Exception as e:
            logger.error("BlockSlot failed for queue %s: %s", queue, e)
            await self.notifier.send(
                f"Slot found on queue {queue}, day {earliest_day}, time {slot_time or slot_id} "
                f"but BLOCKING FAILED ({e}).\nGo grab it manually NOW: {RESERVATION_PAGE_URL}"
            )
            return

        if self.effective_mode == "block":
            await self.notifier.send(
                f"<b>SLOT BLOCKED</b> - queue {queue}, day {earliest_day}, time {slot_time or slot_id}.\n"
                f"It is held for only a few minutes - finish the reservation NOW manually:\n"
                f"{RESERVATION_PAGE_URL}"
            )
            return

        # block_and_fill
        await self._fill_and_confirm(queue, earliest_day, slot_time, slot_id)

    async def _fill_and_confirm(self, queue: str, day: str, slot_time, slot_id):
        try:
            field_defs = await self.client.get_properties_for_slot(slot_id)
        except Exception as e:
            logger.error("GetPropertiesForSlot failed for queue %s: %s", queue, e)
            await self.notifier.send(
                f"<b>SLOT BLOCKED</b> but fetching the form fields FAILED ({e}).\n"
                f"Queue {queue}, day {day}, time {slot_time or slot_id}.\n"
                f"Finish manually NOW: {RESERVATION_PAGE_URL}"
            )
            return

        filled_properties = build_filled_properties(field_defs, self.config.form_data)

        try:
            update_result = await self.client.update_slot_properties(filled_properties)
            logger.info("UpdateSlotProperties result: %s", update_result)
        except Exception as e:
            logger.error("UpdateSlotProperties failed for queue %s: %s", queue, e)
            await self.notifier.send(
                f"<b>SLOT BLOCKED</b> but filling the form FAILED ({e}).\n"
                f"Queue {queue}, day {day}, time {slot_time or slot_id}.\n"
                f"Finish manually NOW: {RESERVATION_PAGE_URL}"
            )
            return

        summary = (
            f"<b>Slot blocked and form filled</b>\n"
            f"Queue: {queue}\nDay: {day}\nTime: {slot_time or slot_id}\n"
            f"Data used: {self.config.form_data}\n"
        )

        if self.config.auto_confirm:
            await self._do_confirm(queue, day, slot_time, slot_id, notify_prefix=summary)
            return

        summary += "\nConfirm the reservation now?"
        result = await self.notifier.ask_confirm_or_release(summary)
        if result == "confirm":
            await self._do_confirm(queue, day, slot_time, slot_id, notify_prefix="")
        elif result == "release":
            logger.info("User chose to release slot for queue %s day %s.", queue, day)
            await self.notifier.send("Released. Resuming polling.")
        elif result == "send_failed":
            # We couldn't even deliver the buttons — don't sit idle. The slot is
            # blocked+filled; the user must finish from the site immediately.
            logger.error("Confirm prompt undeliverable for queue %s day %s.", queue, day)
            await self.notifier.send(
                f"<b>SLOT BLOCKED &amp; FILLED</b> but I couldn't send the confirm buttons.\n"
                f"Queue {queue}, day {day}, time {slot_time or slot_id}.\n"
                f"Finish manually NOW: {RESERVATION_PAGE_URL}"
            )
        else:
            logger.info("Confirm/release button timed out for queue %s day %s.", queue, day)
            await self.notifier.send(
                "No response within 4 minutes - the hold likely expired. Resuming polling."
            )

    async def _do_confirm(self, queue: str, day: str, slot_time, slot_id, notify_prefix: str):
        try:
            result = await self.client.confirm_reservation()
            reservation_id = result.get("reservationId") if isinstance(result, dict) else result
            logger.info("ConfirmReservation success for queue %s: %s", queue, result)
            await self.notifier.send(
                f"{notify_prefix}<b>RESERVATION CONFIRMED</b>\n"
                f"Queue: {queue}\nDay: {day}\nTime: {slot_time or slot_id}\n"
                f"Reservation ID: {reservation_id}\n\n"
                f"IMPORTANT: check your EMAIL and click the confirmation link "
                f"(two-step email confirmation is required)."
            )
            self.stopped = True
        except Exception as e:
            logger.error("ConfirmReservation failed for queue %s: %s", queue, e)
            await self.notifier.send(
                f"{notify_prefix}Form was filled but CONFIRMATION FAILED ({e}).\n"
                f"Queue: {queue}, day {day}, time {slot_time or slot_id}.\n"
                f"Finish manually NOW: {RESERVATION_PAGE_URL}"
            )

    async def run_cycle(self):
        """Runs one full polling cycle (all queues), handling the first
        available queue found (single-flight for block/fill)."""
        try:
            # check_all_queues_once refreshes the page (fresh reCAPTCHA session)
            # before each queue — see the note there for why.
            results = await self.check_all_queues_once()
        except RateLimitedError as e:
            logger.warning("Rate limited: %s - backing off %ss", e, RATE_LIMIT_BACKOFF_SECONDS)
            await asyncio.sleep(RATE_LIMIT_BACKOFF_SECONDS)
            self.consecutive_failures = 0
            return

        compact = ", ".join(
            f"{q}: {'ERR' if r == 'ERROR' else str(len(r)) + ' days'}"
            for q, r in results.items()
        )
        logger.info("Check cycle: %s", compact)
        self.notifier.last_check_summary = compact
        self.notifier.last_check_time = datetime.now(WARSAW_TZ) if WARSAW_TZ else datetime.now()

        self.total_checks += 1
        all_errored = all(v == "ERROR" for v in results.values()) if results else True
        if all_errored:
            self.consecutive_failures += 1
            logger.warning("All queues errored this cycle (%d/%d consecutive).",
                            self.consecutive_failures, MAX_CONSECUTIVE_FAILURES)
        else:
            self.consecutive_failures = 0
            self.total_successful_checks += 1
            self.last_success_time = self.notifier.last_check_time
            if self.stale_alerted:
                # We were in an alerted-stale state and just recovered.
                self.stale_alerted = False
                down_for = self._fmt_duration(self.last_success_time - self._stale_since)
                await self.notifier.send(
                    f"✅ <b>Recovered</b> — checks are succeeding again "
                    f"(was failing for {down_for}). Last result: {compact}"
                )

        for queue, days in results.items():
            if days == "ERROR" or not days:
                continue
            try:
                await self.handle_available_queue(queue, days)
            except Exception as e:
                logger.error("Unhandled error in grab pipeline for queue %s: %s", queue, e, exc_info=True)
                try:
                    await self.notifier.send(
                        f"Unexpected error handling queue {queue} availability ({e}).\n"
                        f"Days seen: {days}\nGo check manually: {RESERVATION_PAGE_URL}"
                    )
                except Exception:
                    pass
            if self.stopped:
                return

    async def _check_watchdog(self):
        """Turn silent failure into a visible Telegram alert, and send optional
        periodic heartbeats. Without this, in notify_only mode a bot that can no
        longer reach the site / solve captchas looks identical to 'no slots'."""
        now = datetime.now(WARSAW_TZ) if WARSAW_TZ else datetime.now()

        # Stale-check alert: no SUCCESSFUL check for too long.
        stale_for = now - self.last_success_time
        threshold = self.config.stale_alert_minutes * 60
        if self.config.stale_alert_minutes > 0 and stale_for.total_seconds() >= threshold:
            if not self.stale_alerted:
                self.stale_alerted = True
                self._stale_since = self.last_success_time
                await self.notifier.send(
                    f"⚠️ <b>Heads up — I can't check right now</b>\n"
                    f"No successful availability check for "
                    f"{self._fmt_duration(stale_for)} (last OK: "
                    f"{self.last_success_time.strftime('%H:%M:%S')}).\n"
                    f"Likely cause: captcha rejections or the site blocking us. "
                    f"I'll keep retrying and tell you when it recovers. "
                    f"Consider checking manually: {RESERVATION_PAGE_URL}"
                )

        # Optional periodic 'still alive' heartbeat.
        if self.config.heartbeat_hours > 0:
            since_hb = (now - self.last_heartbeat_time).total_seconds()
            if since_hb >= self.config.heartbeat_hours * 3600:
                self.last_heartbeat_time = now
                await self.notifier.send(
                    f"💓 Still watching. Uptime {self._fmt_duration(now - self.started_at)}, "
                    f"{self.total_successful_checks}/{self.total_checks} checks OK, "
                    f"{self.total_slots_found} slot-hits so far. Last: {self.notifier.last_check_summary}"
                )

    async def run_forever(self):
        await self.client.start()
        await self.notifier.start()
        if self.config.startup_notify:
            await self.notifier.send(
                f"🟢 <b>Watcher started</b>\n"
                f"Queues: {', '.join(self.config.queues)} | mode: {self.effective_mode}\n"
                f"Captcha: {self.client._captcha_provider} | "
                f"fast window: {self.config.fast_window}\n"
                f"You'll get a message here the moment a slot appears. "
                f"I'll also warn you if I stop being able to check."
            )
        try:
            while not self.stopped:
                if self.notifier.paused:
                    await asyncio.sleep(5)
                    continue

                if not self.client.is_alive:
                    logger.warning("Browser/page appears dead - restarting.")
                    try:
                        await self.client.restart()
                    except Exception as e:
                        logger.error("Failed to restart browser: %s", e)
                        await asyncio.sleep(30)
                        continue

                try:
                    await self.run_cycle()
                except Exception as e:
                    logger.error("Unhandled error in run_cycle: %s", e, exc_info=True)
                    self.consecutive_failures += 1

                if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.warning(
                        "%d consecutive failed cycles - restarting browser.",
                        self.consecutive_failures,
                    )
                    try:
                        await self.client.restart()
                    except Exception as e:
                        logger.error("Browser restart failed: %s", e)
                    self.consecutive_failures = 0

                if self.stopped:
                    break

                # Must never crash the loop — this is the alerting path itself.
                try:
                    await self._check_watchdog()
                except Exception as e:
                    logger.error("Watchdog check failed: %s", e, exc_info=True)

                interval = (
                    self.config.fast_interval_seconds
                    if is_within_fast_window(self.config.fast_window)
                    else self.config.slow_interval_seconds
                )
                sleep_for = jittered(interval, self.config.jitter_percent)
                logger.info("Next check in %.0fs (base %ds ±%d%%).",
                            sleep_for, interval, int(self.config.jitter_percent * 100))
                await asyncio.sleep(sleep_for)

            logger.info("Polling stopped (reservation confirmed). Keeping Telegram alive for %ss to deliver messages.",
                         POST_SUCCESS_KEEPALIVE_SECONDS)
            await asyncio.sleep(POST_SUCCESS_KEEPALIVE_SECONDS)
        finally:
            await self.notifier.stop()
            await self.client.stop()

    async def run_once(self):
        await self.client.start()
        try:
            results = await self.check_all_queues_once()
            print("Check results:")
            for q, r in results.items():
                print(f"  Queue {q}: {r}")
            return results
        finally:
            await self.client.stop()


# =============================================================================
# Entry point
# =============================================================================

async def test_telegram(config: Config):
    notifier = TelegramNotifier(config)
    await notifier.start()
    await notifier.send("Test message from bezkolejki watcher bot. If you see this, Telegram setup works.")
    await asyncio.sleep(2)
    await notifier.stop()
    print("Test message sent (check Telegram).")


async def test_slot(config: Config):
    """Send a SIMULATED 'slot found' alert so you can see exactly what a real
    notification looks like in the group. Sends nothing to the reservation site."""
    notifier = TelegramNotifier(config)
    await notifier.start()
    fake_day = (datetime.now(WARSAW_TZ) if WARSAW_TZ else datetime.now()).strftime("%Y-%m-%d")
    await notifier.send(
        "🧪 <b>TEST — this is NOT a real slot</b>\n\n"
        "<b>Free slot found!</b>\n"
        "Queue: A\n"
        f"Day: {fake_day}\n"
        "Times: 08:15, 08:30, 08:45\n"
        f"{RESERVATION_PAGE_URL}\n\n"
        "(This is what you'll receive when a real slot appears. In block mode "
        "you'd also get a 'SLOT BLOCKED — finish now' message with the hold.)"
    )
    await asyncio.sleep(2)
    await notifier.stop()
    print("Simulated slot notification sent (check Telegram).")


async def healthcheck(config: Config):
    """End-to-end reliability check: browser+site+captcha, then Telegram.
    Prints a per-component PASS/FAIL report and also posts the summary to the
    group so you can confirm the whole chain works before relying on it."""
    results = {}

    # 1. Browser + site + captcha (one real availability check).
    client = BezkolejkiClient(config)
    try:
        await client.start()
        try:
            oid = QUEUE_OPERATION_IDS[config.queues[0]]
            data = await client.get_available_days(oid)
            days = data.get("availableDays", []) if isinstance(data, dict) else []
            results["browser+site"] = (True, "page loaded, Cloudflare cleared")
            results["captcha"] = (True, f"token accepted (provider: {client._captcha_provider})")
            results["availability_api"] = (True, f"queue {config.queues[0]}: {len(days)} day(s) free")
        finally:
            await client.stop()
    except Exception as e:
        # Distinguish captcha failure from other failures where possible.
        msg = str(e)
        results.setdefault("browser+site", (True, "started"))
        if "captcha" in msg.lower() or "400" in msg:
            results["captcha"] = (False, f"token REJECTED — {msg[:120]}")
        else:
            results.setdefault("browser+site", (False, msg[:120]))
        results.setdefault("captcha", (False, "not reached"))
        results.setdefault("availability_api", (False, "not reached"))

    # 2. Telegram send.
    tg_ok = False
    try:
        notifier = TelegramNotifier(config)
        await notifier.start()
        report = "\n".join(
            f"{'✅' if ok else '❌'} {name}: {detail}"
            for name, (ok, detail) in results.items()
        )
        overall = all(ok for ok, _ in results.values())
        await notifier.send(
            f"🩺 <b>Health check</b> — {'ALL GOOD' if overall else 'PROBLEMS FOUND'}\n{report}"
        )
        await asyncio.sleep(2)
        await notifier.stop()
        tg_ok = True
    except Exception as e:
        results["telegram"] = (False, str(e)[:120])
    else:
        results["telegram"] = (True, "message delivered to group")

    print("\n=== Health check ===")
    for name, (ok, detail) in results.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    overall = all(ok for ok, _ in results.values())
    print(f"\nOverall: {'ALL GOOD ✅' if overall else 'PROBLEMS FOUND ❌'}")
    if not overall and any(name == "captcha" and not ok for name, (ok, _) in results.items()):
        print("Hint: captcha rejection on a VPS usually means you need a 2Captcha "
              "key (set TWOCAPTCHA_API_KEY in .env).")
    return 0 if overall else 1


def main():
    parser = argparse.ArgumentParser(description="bezkolejki.eu appointment slot watcher")
    parser.add_argument("--once", action="store_true", help="run a single check cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="never block/book; treat as notify_only")
    parser.add_argument("--test-telegram", action="store_true", help="send a test Telegram message and exit")
    parser.add_argument("--test-slot", action="store_true", help="send a SIMULATED 'slot found' alert and exit")
    parser.add_argument("--healthcheck", action="store_true", help="run end-to-end reliability check (site+captcha+telegram) and exit")
    args = parser.parse_args()

    config = Config()

    if args.test_telegram:
        asyncio.run(test_telegram(config))
        return

    if args.test_slot:
        asyncio.run(test_slot(config))
        return

    if args.healthcheck:
        sys.exit(asyncio.run(healthcheck(config)))

    watcher = Watcher(config, dry_run=args.dry_run)

    if args.once:
        asyncio.run(watcher.run_once())
        return

    asyncio.run(watcher.run_forever())


if __name__ == "__main__":
    main()
