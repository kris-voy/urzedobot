"""Outbound-only Telegram Bot API delivery backed by the SQLite outbox."""
from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

from config import Config
from constants import TELEGRAM_RETRY_SECONDS
from state import StateStore


class TelegramOutbox:
    def __init__(
        self,
        config: Config,
        store: StateStore,
        sender: Callable[[str], bool] | None = None,
    ):
        self.config = config
        self.store = store
        self.sender = sender or self._send_sync
        self._stopping = False

    def enqueue(self, kind: str, dedupe_key: str, text: str) -> bool:
        return self.store.enqueue_outbox(kind, dedupe_key, text)

    def _send_sync(self, text: str) -> bool:
        # The bot token only exists in the request URL and is never logged or
        # persisted. The outbox stores alert text, never credentials.
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
        data = urllib.parse.urlencode(
            {
                "chat_id": self.config.telegram_chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "false",
            }
        ).encode()
        request = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return bool(payload.get("ok"))

    async def deliver_once(self) -> bool:
        row = self.store.due_outbox()
        if not row:
            return False
        attempts = row["attempts"] + 1
        error = None
        try:
            delivered = await asyncio.to_thread(self.sender, row["text"])
        except Exception as exc:
            delivered = False
            error = type(exc).__name__
        if delivered:
            self.store.update_outbox(row["id"], delivered=True, attempts=attempts)
            self.store.event(
                "INFO", "notification.delivered", f"Delivered {row['kind']} alert"
            )
            return True
        error = error or "delivery rejected"
        if attempts <= len(TELEGRAM_RETRY_SECONDS):
            delay = TELEGRAM_RETRY_SECONDS[attempts - 1]
            next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            self.store.update_outbox(
                row["id"],
                delivered=False,
                attempts=attempts,
                next_attempt_at=next_attempt,
                error=error,
            )
            self.store.event(
                "WARNING",
                "notification.retry",
                f"Telegram delivery failed; retry {attempts} scheduled in {delay}s",
            )
        else:
            self.store.update_outbox(
                row["id"], delivered=False, attempts=attempts, error=error
            )
            self.store.event(
                "ERROR",
                "notification.delivery_failed",
                f"Telegram alert is undelivered after {attempts} attempts",
                details={"outbox_id": row["id"], "kind": row["kind"]},
            )
        return True

    async def run(self) -> None:
        self._stopping = False
        while not self._stopping:
            handled = await self.deliver_once()
            await asyncio.sleep(0.2 if handled else 1.0)

    def stop(self) -> None:
        self._stopping = True
