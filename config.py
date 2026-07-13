"""Runtime configuration for the bezkolejki watcher, loaded entirely from
environment variables (.env then applicant.env — see constants.py for the
load_dotenv calls), plus the small pure helpers (window/jitter math) that
only depend on that configuration."""
from __future__ import annotations

import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py<3.9 fallback, not expected here
    ZoneInfo = None  # type: ignore

from constants import QUEUE_OPERATION_IDS

WARSAW_TZ = ZoneInfo("Europe/Warsaw") if ZoneInfo else None


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


def is_within_fast_window(window_str: str, now: Optional[dtime] = None) -> bool:
    """True if `now` (default: current Warsaw local time) falls in the window.

    `now` is settable so callers (tests) don't depend on wall-clock time.
    """
    start, end = parse_fast_window(window_str)
    if now is None:
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
