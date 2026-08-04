"""Inbound Telegram command polling and execution."""
from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

from config import Config
from state import StateStore, parse_time


class TelegramInbox:
    def __init__(
        self,
        config: Config,
        store: StateStore,
        *,
        response_writer: Callable[[str, str, str], bool],
        fetcher: Callable[[int, int], list[dict]] | None = None,
        info_provider: Callable[[], dict[str, Any]] | None = None,
    ):
        self.config = config
        self.store = store
        self.response_writer = response_writer
        self.fetcher = fetcher or self._fetch_sync
        self.info_provider = info_provider or (lambda: {})
        self._stopping = False

    def _fetch_sync(self, offset: int, timeout: int) -> list[dict]:
        query = urllib.parse.urlencode(
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": json.dumps(["message"]),
            }
        )
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/getUpdates?{query}"
        with urllib.request.urlopen(url, timeout=timeout + 10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not payload.get("ok"):
            raise RuntimeError("telegram getUpdates failed")
        return payload.get("result", [])

    @staticmethod
    def _age_seconds(value: str | None) -> int | None:
        parsed = parse_time(value)
        if not parsed:
            return None
        return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))

    @staticmethod
    def _format_age(seconds: int | None) -> str:
        if seconds is None:
            return "unknown"
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m"
        return f"{seconds // 3600}h"

    @staticmethod
    def _runtime_status(snapshot: dict[str, Any]) -> str:
        runtime = snapshot["runtime"]
        if runtime.get("paused"):
            return "paused"
        if runtime.get("heartbeat_at"):
            return "running"
        return "starting"

    @staticmethod
    def _operation_lines(snapshot: dict[str, Any]) -> list[str]:
        lines = []
        for item in snapshot["operations"]:
            lines.append(
                f"{item['prefix']} {item['status']} "
                f"a/s/f={item.get('attempts_total', 0)}/{item.get('successes_total', 0)}/{item.get('failures_total', 0)}"
            )
        return lines

    def _status_text(self) -> str:
        snapshot = self.store.snapshot()
        runtime = snapshot["runtime"]
        heartbeat = self._format_age(self._age_seconds(runtime.get("heartbeat_at")))
        cycle_ok = runtime.get("last_cycle_ok")
        cycle = "ok" if cycle_ok == 1 else "failed" if cycle_ok == 0 else "n/a"
        last_error = runtime.get("last_error") or "-"
        info = self.info_provider() or {}
        lines = [
            f"Node: {self.config.node_name}",
            f"Status: {self._runtime_status(snapshot)}",
            f"Heartbeat: {heartbeat} ago",
            f"Last cycle: {cycle}",
            f"Last error: {last_error}",
        ]
        if info.get("cooldown_remaining"):
            lines.append(f"Cool-down: {int(info['cooldown_remaining'])}s remaining")
        if info.get("captcha_failure_streak"):
            lines.append(
                f"CAPTCHA streak: {info['captcha_failure_streak']} "
                f"(next check in ~{int(info.get('next_interval', 0))}s)"
            )
        lines.extend(self._operation_lines(snapshot))
        return "\n".join(lines)

    def _read_meminfo(self) -> str:
        try:
            with open("/proc/meminfo", encoding="utf-8") as handle:
                data = {
                    line.split(":", 1)[0].strip(): int(line.split(":", 1)[1].strip().split()[0])
                    for line in handle
                    if ":" in line
                }
            total = data.get("MemTotal")
            available = data.get("MemAvailable")
            if total and available:
                return f"{available // 1024}MB/{total // 1024}MB"
        except Exception:
            pass
        return "n/a"

    def _stats_text(self) -> str:
        snapshot = self.store.snapshot()
        runtime = snapshot["runtime"]
        total_attempts = sum(int(item.get("attempts_total") or 0) for item in snapshot["operations"])
        total_success = sum(int(item.get("successes_total") or 0) for item in snapshot["operations"])
        total_failures = sum(int(item.get("failures_total") or 0) for item in snapshot["operations"])
        db_size = os.path.getsize(self.store.path) if os.path.exists(self.store.path) else 0
        disk = shutil.disk_usage(os.path.dirname(self.store.path) or ".")
        uptime = self._format_age(self._age_seconds(runtime.get("started_at")))
        info = self.info_provider() or {}
        lines = [
            f"Node: {self.config.node_name}",
            f"Attempts: {total_attempts} success={total_success} failed={total_failures}",
            *self._operation_lines(snapshot),
            f"DB: {db_size // 1024}KB",
            f"Disk free: {disk.free // (1024 ** 3)}GB/{disk.total // (1024 ** 3)}GB",
            f"Mem free/total: {self._read_meminfo()}",
            f"PID: {runtime.get('pid') or os.getpid()} uptime={uptime}",
            f"Python: {platform.python_version()} ({platform.system()})",
        ]
        solver = info.get("solver") or {}
        if solver.get("enabled"):
            lines.append(
                f"Solver: {solver['provider']} solved={solver['total_solved']}/"
                f"{solver['total_calls']} hour={solver['calls_last_hour']}/{solver['max_per_hour']}"
            )
        else:
            lines.append("Solver: disabled")
        return "\n".join(lines)

    async def _respond(self, update_id: int, kind: str, text: str) -> None:
        dedupe_key = f"{update_id}:{kind}"
        queued = self.response_writer("command.reply", dedupe_key, text)
        if queued:
            self.store.event("INFO", "telegram.reply.queued", f"Queued Telegram reply: {kind}")
        else:
            self.store.event("WARNING", "telegram.reply.duplicate", f"Skipped duplicate reply: {kind}")

    @staticmethod
    def _check_result_summary(result: dict[str, Any] | None) -> str:
        if not isinstance(result, dict):
            return "done; result unavailable"
        healthy = 0
        failed = 0
        for value in result.values():
            if isinstance(value, dict) and value.get("status") == "healthy":
                healthy += 1
            elif isinstance(value, dict) and value.get("status") == "error":
                failed += 1
        return f"healthy={healthy} failed={failed}"

    async def _handle_recheck(self, update_id: int) -> None:
        command_id = self.store.enqueue_command("check")
        self.store.event("INFO", "telegram.command.recheck", "Queued check command from Telegram")
        deadline = time.monotonic() + self.config.telegram_recheck_wait_seconds
        while time.monotonic() < deadline and not self._stopping:
            row = self.store.command(command_id)
            if row and row["status"] in {"completed", "failed"}:
                if row["status"] == "failed":
                    await self._respond(update_id, "recheck", f"/recheck failed: {row.get('error') or 'unknown error'}")
                else:
                    summary = self._check_result_summary(row.get("result"))
                    await self._respond(update_id, "recheck", f"/recheck done ({summary})\n{self._status_text()}")
                return
            await asyncio.sleep(0.5)
        await self._respond(
            update_id,
            "recheck",
            f"/recheck queued (id={command_id}); still running. Use /status.",
        )

    async def _handle_command(self, update_id: int, text: str) -> None:
        command = text.split()[0].lower()
        if command == "/status":
            self.store.event("INFO", "telegram.command.status", "Status requested from Telegram")
            await self._respond(update_id, "status", self._status_text())
        elif command == "/stats":
            self.store.event("INFO", "telegram.command.stats", "Stats requested from Telegram")
            await self._respond(update_id, "stats", self._stats_text())
        elif command == "/recheck":
            await self._handle_recheck(update_id)
        else:
            self.store.event("WARNING", "telegram.command.unsupported", f"Unsupported Telegram command: {command}")
            await self._respond(update_id, "unsupported", "Supported commands: /status /recheck /stats")

    async def _process_update(self, update: dict[str, Any]) -> None:
        update_id = int(update.get("update_id", 0))
        message = update.get("message") or {}
        text = str(message.get("text") or "").strip()
        chat_id = str((message.get("chat") or {}).get("id", ""))
        if not text.startswith("/"):
            return
        if chat_id != self.config.telegram_chat_id:
            self.store.event("WARNING", "telegram.command.unauthorized", "Ignored Telegram command from unauthorized chat")
            return
        await self._handle_command(update_id, text)

    async def poll_once(self) -> int:
        current_offset = self.store.telegram_offset()
        offset = current_offset + 1
        updates = await asyncio.to_thread(
            self.fetcher, offset, self.config.telegram_poll_timeout_seconds
        )
        processed = 0
        for update in sorted(updates, key=lambda item: int(item.get("update_id", 0))):
            update_id = int(update.get("update_id", 0))
            try:
                await self._process_update(update)
            finally:
                current_offset = max(current_offset, update_id)
                self.store.set_telegram_offset(current_offset)
            processed += 1
        return processed

    async def run(self) -> None:
        self._stopping = False
        while not self._stopping:
            try:
                await self.poll_once()
            except Exception as exc:
                self.store.event(
                    "ERROR",
                    "telegram.poll.failed",
                    f"Telegram polling failed: {type(exc).__name__}",
                )
                await asyncio.sleep(self.config.telegram_poll_backoff_seconds)

    def stop(self) -> None:
        self._stopping = True
