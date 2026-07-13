"""Dedup tracker for notify_only mode."""
from __future__ import annotations

import time


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
