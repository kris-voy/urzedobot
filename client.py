"""Read-only Playwright client for the verified bezkolejki availability APIs."""
from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from config import Config
from constants import (
    API_TIMEOUT_MS,
    BASE_URL,
    CAPTCHA_MINT_SPACING_SECONDS,
    CAPTCHA_TOKEN_PARAM,
    COMPANY_NAME,
    HCAPTCHA_CHALLENGE_MARKERS,
    HCAPTCHA_EXECUTE_TIMEOUT_MS,
    HCAPTCHA_READY_TIMEOUT_MS,
    HCAPTCHA_SITE_KEY,
    RESERVATION_PAGE_URL,
    SAFE_GET_RETRIES,
    SERVICE_NAME_TEMPLATE,
    SITE_CONFIG_URL,
    UUID_PATTERN,
)
from captcha_solver import CaptchaSolver
from errors import (
    ApiError,
    CaptchaChallengeError,
    CaptchaContractError,
    CaptchaError,
    CaptchaSolverError,
    RateLimitedError,
    SchemaError,
)


def is_challenge_failure(message: str) -> bool:
    """True when hCaptcha refused a passive token and demanded interaction."""
    lowered = (message or "").lower()
    return any(marker in lowered for marker in HCAPTCHA_CHALLENGE_MARKERS)



def parse_retry_after(value: str | None) -> float:
    if not value:
        return 5.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return 5.0


def parse_catalog(payload: Any, prefixes: list[str]) -> dict[str, dict]:
    if isinstance(payload, dict):
        locations = [payload]
    elif isinstance(payload, list) and payload:
        locations = payload
    else:
        raise SchemaError("catalog must contain at least one localization")
    matches: dict[str, dict] = {}
    for location in locations:
        if not isinstance(location, dict) or not isinstance(location.get("queues"), list):
            raise SchemaError("catalog localization must contain a queues array")
        for queue in location["queues"]:
            if not isinstance(queue, dict) or not isinstance(queue.get("operations"), list):
                raise SchemaError("catalog queue must contain an operations array")
            for operation in queue["operations"]:
                if not isinstance(operation, dict):
                    raise SchemaError("catalog operation must be an object")
                prefix = operation.get("prefix")
                if prefix not in prefixes:
                    continue
                expected_name = SERVICE_NAME_TEMPLATE.format(prefix=prefix)
                if operation.get("name") != expected_name:
                    raise SchemaError(f"catalog service name mismatch for {prefix}")
                if operation.get("isReservationActive") is not True:
                    raise SchemaError(f"catalog reservation is inactive for {prefix}")
                if not isinstance(operation.get("id"), int):
                    raise SchemaError(f"catalog operation id is invalid for {prefix}")
                if prefix in matches:
                    raise SchemaError(f"catalog contains duplicate operation for {prefix}")
                matches[prefix] = {
                    "id": operation["id"],
                    "prefix": prefix,
                    "name": operation["name"],
                    "isReservationActive": True,
                }
    missing = [prefix for prefix in prefixes if prefix not in matches]
    if missing:
        raise SchemaError(f"catalog is missing validated operations: {', '.join(missing)}")
    return matches


def parse_availability(payload: Any, expected_operation_id: int) -> list[str]:
    required = {"operationId", "availableDays", "disabledDays", "minDate", "maxDate"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise SchemaError("availability response does not match the captured schema")
    if payload["operationId"] != expected_operation_id:
        raise SchemaError("availability operationId does not match the requested operation")
    if not isinstance(payload["availableDays"], list) or not all(
        isinstance(day, str) and day for day in payload["availableDays"]
    ):
        raise SchemaError("availableDays must be an array of non-empty strings")
    if not isinstance(payload["disabledDays"], list):
        raise SchemaError("disabledDays must be an array")
    if payload["minDate"] is not None and not isinstance(payload["minDate"], str):
        raise SchemaError("minDate must be a string or null")
    if payload["maxDate"] is not None and not isinstance(payload["maxDate"], str):
        raise SchemaError("maxDate must be a string or null")
    return payload["availableDays"]


def parse_slots(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        slots = payload
    elif isinstance(payload, dict):
        keys = [key for key in ("availableSlots", "slots", "times") if key in payload]
        if len(keys) != 1:
            raise SchemaError("slot response must contain exactly one supported envelope")
        slots = payload[keys[0]]
    else:
        raise SchemaError("slot response must be an array or supported envelope")
    if not isinstance(slots, list):
        raise SchemaError("slot envelope must contain an array")
    result = []
    for slot in slots:
        if (
            not isinstance(slot, dict)
            or slot.get("id") in (None, "")
            or not isinstance(slot.get("dateTime"), str)
            or not slot["dateTime"]
        ):
            raise SchemaError("each slot must contain id and dateTime")
        result.append({"id": slot["id"], "dateTime": slot["dateTime"]})
    return result


class BezkolejkiClient:
    def __init__(self, config: Config, solver: CaptchaSolver | None = None):
        self.config = config
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._auth_token: str | None = None
        self._last_captcha_mint = 0.0
        self._sitekey: str | None = None
        self._widget_id: str | None = None
        self.solver = solver or CaptchaSolver(
            config.captcha_solver_provider,
            config.captcha_solver_api_key,
            timeout_seconds=config.captcha_solver_timeout_seconds,
            max_per_hour=config.captcha_solver_max_per_hour,
        )
        self.solver_engaged = False
        self.last_challenge_reason: str | None = None

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        launch: dict[str, Any] = {"headless": self.config.headless}
        # Raspberry Pi / arm64 has no bundled Playwright Chromium, so allow the
        # distro browser to be used instead.
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "").strip()
        if executable:
            launch["executable_path"] = executable
        self._browser = await self._playwright.chromium.launch(**launch)

    async def stop(self) -> None:
        await self._close_context()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._browser = None
        self._playwright = None
        self._auth_token = None

    async def begin_cycle(self) -> None:
        """Open a fresh context for catalog discovery (service-level)."""
        if not self._browser:
            raise RuntimeError("browser is not started")
        await self._close_context()
        self._context = await self._browser.new_context(locale="pl-PL")
        await self._open_page()
        self._auth_token = None

    async def begin_queue(self) -> None:
        """Open a brand-new browser context for one queue's availability check.

        Each queue gets its own isolated context so that every CAPTCHA mint
        is a first-mint in a clean session — the root cause of B and C failing
        in the old shared-context approach.
        """
        if not self._browser:
            raise RuntimeError("browser is not started")
        await self._close_context()
        # Deliberately use Chromium's native identity: no UA override, stealth
        # script, proxy/profile rotation, or launch-time masking arguments.
        self._context = await self._browser.new_context(locale="pl-PL")
        await self._open_page()
        self._auth_token = None
        self._last_captcha_mint = 0.0

    async def _close_context(self) -> None:
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
            self._page = None
        self._widget_id = None

    async def _open_page(self) -> None:
        assert self._context
        if self._page and not self._page.is_closed():
            await self._page.close()
        self._page = await self._context.new_page()
        self._page.set_default_timeout(API_TIMEOUT_MS)
        await self._page.goto(
            RESERVATION_PAGE_URL,
            wait_until="domcontentloaded",
            timeout=API_TIMEOUT_MS,
        )

    async def _discover_sitekey(self) -> str:
        """Resolve the live hCaptcha sitekey, falling back to the pinned constant.

        Reading it at runtime means a sitekey rotation by the site is a non-event
        instead of a total outage.
        """
        if self._sitekey:
            return self._sitekey
        assert self._page
        candidates: list[str] = []
        try:
            found = await self._page.evaluate(
                """
                () => {
                    const out = [];
                    for (const el of document.querySelectorAll('[data-sitekey]')) {
                        out.push(el.getAttribute('data-sitekey'));
                    }
                    for (const script of document.querySelectorAll('script[src]')) {
                        const match = script.src.match(/[?&](sitekey|render)=([^&]+)/);
                        if (match) out.push(decodeURIComponent(match[2]));
                    }
                    if (window.hcaptchaSiteKey) out.push(window.hcaptchaSiteKey);
                    return out;
                }
                """
            )
            candidates.extend(item for item in (found or []) if isinstance(item, str))
        except Exception:
            pass
        if not any(re.fullmatch(UUID_PATTERN, item) for item in candidates):
            try:
                config_js = await self._page.evaluate(
                    """
                    async url => {
                        const response = await fetch(url, {cache: 'no-store'});
                        return response.ok ? await response.text() : '';
                    }
                    """,
                    SITE_CONFIG_URL,
                )
                candidates.extend(re.findall(UUID_PATTERN, config_js or ""))
            except Exception:
                pass
        for candidate in candidates:
            if re.fullmatch(UUID_PATTERN, candidate):
                self._sitekey = candidate
                return candidate
        self._sitekey = HCAPTCHA_SITE_KEY
        return self._sitekey

    async def _ensure_hcaptcha(self) -> None:
        """Wait for the page's own hCaptcha API. We never inject our own script."""
        assert self._page
        try:
            await self._page.wait_for_function(
                "() => window.hcaptcha && window.hcaptcha.render && window.hcaptcha.execute",
                timeout=HCAPTCHA_READY_TIMEOUT_MS,
            )
        except Exception as exc:
            has_grecaptcha = False
            try:
                has_grecaptcha = bool(
                    await self._page.evaluate("() => !!(window.grecaptcha)")
                )
            except Exception:
                pass
            detail = (
                "page exposes grecaptcha but not hcaptcha"
                if has_grecaptcha
                else "page exposes no usable hcaptcha API"
            )
            raise CaptchaContractError(detail) from exc

    async def _ensure_widget(self) -> str:
        """Render one invisible hCaptcha widget per browser context and cache its id."""
        assert self._page
        if self._widget_id is not None:
            return self._widget_id
        await self._ensure_hcaptcha()
        sitekey = await self._discover_sitekey()
        try:
            widget_id = await self._page.evaluate(
                """
                sitekey => {
                    let host = document.getElementById('sv-hcaptcha-host');
                    if (!host) {
                        host = document.createElement('div');
                        host.id = 'sv-hcaptcha-host';
                        host.style.display = 'none';
                        document.body.appendChild(host);
                    }
                    return window.hcaptcha.render(host, {
                        sitekey: sitekey,
                        size: 'invisible',
                    });
                }
                """,
                sitekey,
            )
        except Exception as exc:
            raise CaptchaContractError(f"hcaptcha.render failed: {exc}") from exc
        if widget_id is None:
            raise CaptchaContractError("hcaptcha.render returned no widget id")
        self._widget_id = str(widget_id)
        return self._widget_id

    async def _mint_native(self) -> str:
        assert self._page
        widget_id = await self._ensure_widget()
        try:
            response = await self._page.evaluate(
                """
                async ({widgetId, timeout}) => {
                    try {
                        const result = await window.hcaptcha.execute(widgetId, {async: true});
                        return {ok: true, token: (result && result.response) || result};
                    } catch (err) {
                        return {ok: false, error: String((err && err.message) || err)};
                    }
                }
                """,
                {"widgetId": widget_id, "timeout": HCAPTCHA_EXECUTE_TIMEOUT_MS},
            )
        except Exception as exc:
            message = str(exc)
            if is_challenge_failure(message):
                raise CaptchaChallengeError(message) from exc
            raise CaptchaContractError(f"hcaptcha.execute failed: {message}") from exc
        if not isinstance(response, dict) or not response.get("ok"):
            detail = (response or {}).get("error", "unknown hcaptcha error")
            if is_challenge_failure(detail):
                raise CaptchaChallengeError(detail)
            raise CaptchaContractError(f"hcaptcha.execute failed: {detail}")
        token = response.get("token")
        if not isinstance(token, str) or not token:
            raise CaptchaChallengeError("hcaptcha returned an empty token")
        # Reset the widget so the next execute is a fresh challenge rather than a
        # replay of a token the server has already consumed.
        try:
            await self._page.evaluate(
                "widgetId => window.hcaptcha.reset(widgetId)", widget_id
            )
        except Exception:
            self._widget_id = None
        return token

    async def _captcha(self, action: str) -> str:
        assert self._page
        elapsed = time.monotonic() - self._last_captcha_mint
        if elapsed < CAPTCHA_MINT_SPACING_SECONDS:
            await asyncio.sleep(CAPTCHA_MINT_SPACING_SECONDS - elapsed)
        try:
            token = await self._mint_native()
        except CaptchaChallengeError as challenge:
            self.last_challenge_reason = str(challenge)
            if not self.solver.enabled:
                raise
            try:
                token = await self.solver.solve(
                    await self._discover_sitekey(), RESERVATION_PAGE_URL
                )
            except CaptchaSolverError as exc:
                raise CaptchaChallengeError(
                    f"{challenge.detail}; solver fallback failed: {exc.detail}"
                ) from exc
            self.solver_engaged = True
        self._last_captcha_mint = time.monotonic()
        return token


    async def _request_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        authenticated: bool = False,
        captcha_action: str | None = None,
    ) -> Any:
        assert self._page
        token = await self._ensure_auth_token() if authenticated else None
        safe_params = dict(params or {})
        if captcha_action:
            safe_params[CAPTCHA_TOKEN_PARAM] = await self._captcha(captcha_action)
        result = await self._page.evaluate(
            """
            async args => {
                const url = new URL(args.baseUrl + args.path);
                for (const [key, value] of Object.entries(args.params)) {
                    url.searchParams.set(key, String(value));
                }
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), args.timeout);
                try {
                    const headers = args.token ? {Authorization: 'Bearer ' + args.token} : {};
                    const response = await fetch(url, {
                        method: 'GET', headers, signal: controller.signal,
                    });
                    let body = null;
                    let json = true;
                    try { body = await response.json(); } catch (_) { json = false; }
                    return {
                        status: response.status,
                        body,
                        json,
                        retryAfter: response.headers.get('Retry-After'),
                    };
                } finally {
                    clearTimeout(timer);
                }
            }
            """,
            {
                "baseUrl": BASE_URL,
                "path": path,
                "params": safe_params,
                "token": token,
                "timeout": API_TIMEOUT_MS,
            },
        )
        status = result["status"]
        if status == 429:
            raise RateLimitedError(path, parse_retry_after(result.get("retryAfter")))
        if status == 400 and captcha_action:
            detail = result.get("body")
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("Message") or detail
            reason = str(detail).strip() if detail not in (None, "") else "rejected token"
            raise CaptchaError(f"{path} ({reason[:120]})", status)
        if status >= 400 or not result.get("json"):
            raise ApiError(path, status)
        return result["body"]

    async def _safe_get(self, *args: Any, **kwargs: Any) -> Any:
        for attempt in range(SAFE_GET_RETRIES + 1):
            try:
                return await self._request_json(*args, **kwargs)
            except RateLimitedError as exc:
                if attempt >= SAFE_GET_RETRIES:
                    raise
                await asyncio.sleep(exc.retry_after)
            except CaptchaError:
                # Never retry captcha failures: a rejected or challenged token is
                # not transient, and retrying multiplies the request volume that
                # damaged our reputation in the first place.
                raise
            except (ApiError, TimeoutError, PlaywrightTimeoutError, PlaywrightError):
                if attempt >= SAFE_GET_RETRIES:
                    raise
                await asyncio.sleep(1)

    async def _ensure_auth_token(self) -> str:
        if self._auth_token:
            return self._auth_token
        body = await self._safe_get(f"/Authentication/GetEmptyToken/{COMPANY_NAME}")
        if not isinstance(body, dict) or not isinstance(body.get("token"), str) or not body["token"]:
            raise SchemaError("authentication response does not contain a token")
        self._auth_token = body["token"]
        return self._auth_token

    async def discover_catalog(self, prefixes: list[str]) -> dict[str, dict]:
        body = await self._safe_get(
            f"/Operation/GetReservationLocalizations/{COMPANY_NAME}",
            authenticated=True,
        )
        return parse_catalog(body, prefixes)

    async def get_available_days(self, operation_id: int) -> list[str]:
        body = await self._safe_get(
            "/Slot/GetAvailableDaysForOperation",
            {"companyName": COMPANY_NAME, "lastStepId": operation_id},
            authenticated=True,
            captcha_action="GetAvailableDaysForOperation",
        )
        return parse_availability(body, operation_id)

    async def get_available_slots(self, operation_id: int, day: str) -> list[dict]:
        body = await self._safe_get(
            "/Slot/GetAvailableSlotsForOperationAndDay",
            {
                "companyName": COMPANY_NAME,
                "lastStepId": operation_id,
                "day": day,
            },
            authenticated=True,
            captcha_action="GetAvailableSlotsForOperationAndDay",
        )
        return parse_slots(body)
