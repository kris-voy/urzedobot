"""Optional third-party hCaptcha solving, used only when the site refuses passive tokens.

Disabled by default (`CAPTCHA_SOLVER_PROVIDER=none`). When enabled it is a *fallback*:
the watcher always tries a free native mint first and only pays for a solve when
hCaptcha insists on an interactive challenge. An hourly cap bounds spend.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from typing import Any

from constants import SUPPORTED_CAPTCHA_SOLVERS
from errors import CaptchaSolverError

CAPSOLVER_BASE = "https://api.capsolver.com"
TWOCAPTCHA_BASE = "https://api.2captcha.com"
POLL_INTERVAL_SECONDS = 5.0
SUPPORTED_PROVIDERS = SUPPORTED_CAPTCHA_SOLVERS


def _post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raise CaptchaSolverError(f"{url} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CaptchaSolverError(f"{url} unreachable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CaptchaSolverError(f"{url} returned non-JSON body") from exc


class CaptchaSolver:
    """Provider-agnostic hCaptcha solver with a hard hourly spend cap."""

    def __init__(
        self,
        provider: str,
        api_key: str,
        *,
        timeout_seconds: int = 120,
        max_per_hour: int = 60,
        clock=time.monotonic,
    ):
        provider = (provider or "none").strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"CAPTCHA_SOLVER_PROVIDER must be one of {', '.join(SUPPORTED_PROVIDERS)}"
            )
        self.provider = provider
        self.api_key = api_key.strip()
        self.timeout_seconds = timeout_seconds
        self.max_per_hour = max_per_hour
        self._clock = clock
        self._calls: list[float] = []
        self.total_calls = 0
        self.total_solved = 0
        self.total_failed = 0

    @property
    def enabled(self) -> bool:
        return self.provider != "none" and bool(self.api_key)

    def _prune(self) -> None:
        cutoff = self._clock() - 3600
        self._calls = [stamp for stamp in self._calls if stamp > cutoff]

    def calls_last_hour(self) -> int:
        self._prune()
        return len(self._calls)

    def capacity_available(self) -> bool:
        return self.calls_last_hour() < self.max_per_hour

    def stats(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "enabled": self.enabled,
            "calls_last_hour": self.calls_last_hour(),
            "max_per_hour": self.max_per_hour,
            "total_calls": self.total_calls,
            "total_solved": self.total_solved,
            "total_failed": self.total_failed,
        }

    async def solve(self, sitekey: str, page_url: str) -> str:
        if not self.enabled:
            raise CaptchaSolverError("no solver provider configured")
        if not self.capacity_available():
            raise CaptchaSolverError(
                f"hourly cap reached ({self.max_per_hour} solves/hour)"
            )
        self._calls.append(self._clock())
        self.total_calls += 1
        try:
            token = await asyncio.get_running_loop().run_in_executor(
                None, self._solve_sync, sitekey, page_url
            )
        except CaptchaSolverError:
            self.total_failed += 1
            raise
        if not token:
            self.total_failed += 1
            raise CaptchaSolverError("solver returned an empty token")
        self.total_solved += 1
        return token

    def _solve_sync(self, sitekey: str, page_url: str) -> str:
        if self.provider == "capsolver":
            return self._solve_capsolver(sitekey, page_url)
        return self._solve_2captcha(sitekey, page_url)

    def _deadline(self) -> float:
        return time.monotonic() + self.timeout_seconds

    def _solve_capsolver(self, sitekey: str, page_url: str) -> str:
        created = _post_json(
            f"{CAPSOLVER_BASE}/createTask",
            {
                "clientKey": self.api_key,
                "task": {
                    "type": "HCaptchaTaskProxyless",
                    "websiteURL": page_url,
                    "websiteKey": sitekey,
                },
            },
            timeout=30,
        )
        if created.get("errorId"):
            raise CaptchaSolverError(
                f"capsolver createTask: {created.get('errorDescription') or created}"
            )
        task_id = created.get("taskId")
        if not task_id:
            raise CaptchaSolverError("capsolver createTask returned no taskId")
        deadline = self._deadline()
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_SECONDS)
            result = _post_json(
                f"{CAPSOLVER_BASE}/getTaskResult",
                {"clientKey": self.api_key, "taskId": task_id},
                timeout=30,
            )
            if result.get("errorId"):
                raise CaptchaSolverError(
                    f"capsolver getTaskResult: {result.get('errorDescription') or result}"
                )
            if result.get("status") == "ready":
                return (result.get("solution") or {}).get("gRecaptchaResponse", "")
        raise CaptchaSolverError(f"capsolver timed out after {self.timeout_seconds}s")

    def _solve_2captcha(self, sitekey: str, page_url: str) -> str:
        created = _post_json(
            f"{TWOCAPTCHA_BASE}/createTask",
            {
                "clientKey": self.api_key,
                "task": {
                    "type": "HCaptchaTaskProxyless",
                    "websiteURL": page_url,
                    "websiteKey": sitekey,
                },
            },
            timeout=30,
        )
        if created.get("errorId"):
            raise CaptchaSolverError(
                f"2captcha createTask: {created.get('errorDescription') or created}"
            )
        task_id = created.get("taskId")
        if not task_id:
            raise CaptchaSolverError("2captcha createTask returned no taskId")
        deadline = self._deadline()
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_SECONDS)
            result = _post_json(
                f"{TWOCAPTCHA_BASE}/getTaskResult",
                {"clientKey": self.api_key, "taskId": task_id},
                timeout=30,
            )
            if result.get("errorId"):
                raise CaptchaSolverError(
                    f"2captcha getTaskResult: {result.get('errorDescription') or result}"
                )
            if result.get("status") == "ready":
                return (result.get("solution") or {}).get("token", "")
        raise CaptchaSolverError(f"2captcha timed out after {self.timeout_seconds}s")
