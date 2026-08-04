from __future__ import annotations

import os
import random
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from constants import QUEUE_PREFIXES, QUEUE_STAGGER_SECONDS, SUPPORTED_CAPTCHA_SOLVERS

load_dotenv(Path(__file__).with_name(".env"))
WARSAW_TZ = ZoneInfo("Europe/Warsaw")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def _integer(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    try:
        return int(value) if value else default
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _number(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    try:
        return float(value) if value else default
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


@dataclass
class Config:
    mode: str = field(default_factory=lambda: _env("MODE", "notify_only").strip().lower())
    auto_confirm: bool = field(default_factory=lambda: _bool("AUTO_CONFIRM", False))
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN").strip())
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID").strip())
    queues: list[str] = field(default_factory=lambda: [
        item.strip().upper() for item in _env("QUEUES", "A,B,C").split(",") if item.strip()
    ])
    database_path: str = field(default_factory=lambda: _env("DATABASE_PATH", "/app/data/sv.db"))
    fast_window: str = field(default_factory=lambda: _env("FAST_WINDOW", "05:45-08:45"))
    fast_interval_seconds: int = field(default_factory=lambda: _integer("FAST_INTERVAL_SECONDS", 30))
    slow_interval_seconds: int = field(default_factory=lambda: _integer("SLOW_INTERVAL_SECONDS", 300))
    jitter_percent: float = field(default_factory=lambda: _number("JITTER_PERCENT", 0.35))
    headless: bool = field(default_factory=lambda: _bool("HEADLESS", True))
    incident_threshold: int = field(default_factory=lambda: _integer("INCIDENT_THRESHOLD", 3))
    heartbeat_stale_seconds: int = field(default_factory=lambda: _integer("HEARTBEAT_STALE_SECONDS", 90))
    catalog_stale_seconds: int = field(default_factory=lambda: _integer("CATALOG_STALE_SECONDS", 86400))
    operation_stale_seconds: int = field(default_factory=lambda: _integer("OPERATION_STALE_SECONDS", 900))
    telegram_poll_timeout_seconds: int = field(default_factory=lambda: _integer("TELEGRAM_POLL_TIMEOUT_SECONDS", 15))
    telegram_poll_backoff_seconds: int = field(default_factory=lambda: _integer("TELEGRAM_POLL_BACKOFF_SECONDS", 3))
    telegram_recheck_wait_seconds: int = field(default_factory=lambda: _integer("TELEGRAM_RECHECK_WAIT_SECONDS", 20))
    node_name: str = field(default_factory=lambda: _env("NODE_NAME", socket.gethostname()).strip())
    schedule_offset_seconds: int = field(default_factory=lambda: _integer("SCHEDULE_OFFSET_SECONDS", 0))
    captcha_backoff_max_seconds: int = field(default_factory=lambda: _integer("CAPTCHA_BACKOFF_MAX_SECONDS", 1800))
    captcha_cooldown_failures: int = field(default_factory=lambda: _integer("CAPTCHA_COOLDOWN_FAILURES", 10))
    captcha_cooldown_seconds: int = field(default_factory=lambda: _integer("CAPTCHA_COOLDOWN_SECONDS", 2700))
    captcha_solver_provider: str = field(default_factory=lambda: _env("CAPTCHA_SOLVER_PROVIDER", "none").strip().lower())
    captcha_solver_api_key: str = field(default_factory=lambda: _env("CAPTCHA_SOLVER_API_KEY").strip())
    captcha_solver_timeout_seconds: int = field(default_factory=lambda: _integer("CAPTCHA_SOLVER_TIMEOUT_SECONDS", 120))
    captcha_solver_max_per_hour: int = field(default_factory=lambda: _integer("CAPTCHA_SOLVER_MAX_PER_HOUR", 60))
    daily_digest_hour: int = field(default_factory=lambda: _integer("DAILY_DIGEST_HOUR", 21))
    queue_stagger_seconds: float = field(default_factory=lambda: _number("QUEUE_STAGGER_SECONDS", QUEUE_STAGGER_SECONDS))

    def __post_init__(self) -> None:
        if self.mode != "notify_only":
            raise ValueError("MODE must be notify_only; blocking and form filling are not supported")
        if self.auto_confirm:
            raise ValueError("AUTO_CONFIRM is not supported in notify-only mode")
        if not self.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required for urgent alerts")
        if not self.telegram_chat_id:
            raise ValueError("TELEGRAM_CHAT_ID is required for urgent alerts")
        if not self.queues or any(queue not in QUEUE_PREFIXES for queue in self.queues):
            raise ValueError("QUEUES must contain only A, B, and C")
        if len(set(self.queues)) != len(self.queues):
            raise ValueError("QUEUES must not contain duplicates")
        parse_fast_window(self.fast_window)
        if self.fast_interval_seconds < 1 or self.slow_interval_seconds < 1:
            raise ValueError("poll intervals must be positive")
        if not 0 <= self.jitter_percent <= 1:
            raise ValueError("JITTER_PERCENT must be between 0 and 1")
        if self.incident_threshold < 1:
            raise ValueError("INCIDENT_THRESHOLD must be positive")
        if self.telegram_poll_timeout_seconds < 1:
            raise ValueError("TELEGRAM_POLL_TIMEOUT_SECONDS must be positive")
        if self.telegram_poll_backoff_seconds < 1:
            raise ValueError("TELEGRAM_POLL_BACKOFF_SECONDS must be positive")
        if self.telegram_recheck_wait_seconds < 1:
            raise ValueError("TELEGRAM_RECHECK_WAIT_SECONDS must be positive")
        if not self.node_name:
            raise ValueError("NODE_NAME must not be empty")
        if self.schedule_offset_seconds < 0:
            raise ValueError("SCHEDULE_OFFSET_SECONDS must not be negative")
        if self.captcha_backoff_max_seconds < 1:
            raise ValueError("CAPTCHA_BACKOFF_MAX_SECONDS must be positive")
        if self.captcha_cooldown_failures < 1:
            raise ValueError("CAPTCHA_COOLDOWN_FAILURES must be positive")
        if self.captcha_cooldown_seconds < 1:
            raise ValueError("CAPTCHA_COOLDOWN_SECONDS must be positive")
        if self.captcha_solver_provider not in SUPPORTED_CAPTCHA_SOLVERS:
            raise ValueError(
                "CAPTCHA_SOLVER_PROVIDER must be one of "
                + ", ".join(SUPPORTED_CAPTCHA_SOLVERS)
            )
        if self.captcha_solver_provider != "none" and not self.captcha_solver_api_key:
            raise ValueError("CAPTCHA_SOLVER_API_KEY is required when a solver is enabled")
        if self.captcha_solver_timeout_seconds < 1:
            raise ValueError("CAPTCHA_SOLVER_TIMEOUT_SECONDS must be positive")
        if self.captcha_solver_max_per_hour < 0:
            raise ValueError("CAPTCHA_SOLVER_MAX_PER_HOUR must not be negative")
        if not 0 <= self.daily_digest_hour <= 23:
            raise ValueError("DAILY_DIGEST_HOUR must be between 0 and 23")
        if self.queue_stagger_seconds < 0:
            raise ValueError("QUEUE_STAGGER_SECONDS must not be negative")


def parse_fast_window(window: str) -> tuple[dtime, dtime]:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", window)
    if not match:
        raise ValueError(f"Invalid FAST_WINDOW: {window!r}")
    try:
        return dtime(int(match[1]), int(match[2])), dtime(int(match[3]), int(match[4]))
    except ValueError as exc:
        raise ValueError(f"Invalid FAST_WINDOW: {window!r}") from exc


def is_within_fast_window(window: str, now: Optional[dtime] = None) -> bool:
    start, end = parse_fast_window(window)
    now = now or datetime.now(WARSAW_TZ).time()
    return start <= now <= end if start <= end else now >= start or now <= end


def jittered(seconds: float, pct: float) -> float:
    return max(1.0, seconds + random.uniform(-seconds * pct, seconds * pct))
