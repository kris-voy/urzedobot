"""Single-owner watcher loop for catalog discovery, checks, and commands."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from client import BezkolejkiClient
from config import Config, WARSAW_TZ, is_within_fast_window, jittered
from constants import (
    CAPTCHA_BACKOFF_MULTIPLIER,
    MAX_ALERT_LEVEL,
    RESERVATION_PAGE_URL,
)
from errors import CaptchaChallengeError, CaptchaContractError, CaptchaError
from notifier import TelegramOutbox
from state import StateStore
from telegram_inbox import TelegramInbox

logger = logging.getLogger("sv")


class Watcher:
    def __init__(
        self,
        config: Config,
        *,
        store: StateStore | None = None,
        client: BezkolejkiClient | None = None,
        notifier: TelegramOutbox | None = None,
    ):
        self.config = config
        self.store = store or StateStore(config.database_path)
        self.client = client or BezkolejkiClient(config)
        self.notifier = notifier or TelegramOutbox(config, self.store)
        self.inbox = TelegramInbox(
            config,
            self.store,
            response_writer=self.notifier.enqueue,
            info_provider=self.runtime_info,
        )
        self._stopping = False
        self.captcha_failure_streak = 0
        self.cooldown_until = 0.0
        self._contract_alerted = False
        self._solver_alerted = False
        self._last_digest_day: str | None = None

    def runtime_info(self) -> dict[str, Any]:
        return {
            "node": self.config.node_name,
            "captcha_failure_streak": self.captcha_failure_streak,
            "cooldown_remaining": max(0.0, self.cooldown_until - time.monotonic()),
            "next_interval": self.next_interval_seconds(),
            "solver": self.client.solver.stats(),
        }

    @property
    def node(self) -> str:
        return self.config.node_name

    def _label(self, text: str) -> str:
        return f"[{self.node}] {text}"

    def _enqueue_incident(self, incident: dict | None) -> None:
        if not incident:
            return
        scope = incident["scope"]
        qualifier = "service" if scope == "service" else f"operation {scope}"
        if incident.get("reminder"):
            headline = "Availability checker still down"
        elif incident["escalation"]:
            headline = "Availability checker outage escalated"
        else:
            headline = "Availability checker outage"
        text = (
            f"⚠️ <b>{self._label(headline)}</b>\n"
            f"Affected: {qualifier}\n"
            f"Consecutive failures: {incident['count']}\n"
            f"Reason: {incident.get('reason', 'unknown')}\n"
            f"Manual booking: {RESERVATION_PAGE_URL}"
        )
        key = f"{incident['incident_id']}:{incident['level']}"
        if incident.get("reminder"):
            key = f"{key}:{incident.get('stamp', '')}"
        self.notifier.enqueue("operation.outage", key, text)

    def _enqueue_contract_alert(self, detail: str) -> None:
        """A CAPTCHA contract change means the watcher is blind until code changes.

        This is alerted once per process, separately from routine outages, because
        no amount of retrying or waiting will fix it.
        """
        if self._contract_alerted:
            return
        self._contract_alerted = True
        text = (
            f"🛑 <b>{self._label('CAPTCHA contract changed — watcher is blind')}</b>\n"
            f"Detail: {detail}\n"
            "The site changed its CAPTCHA integration. Availability checks cannot "
            "succeed until the watcher code is updated.\n"
            f"Check manually: {RESERVATION_PAGE_URL}"
        )
        self.notifier.enqueue("captcha.contract_changed", f"{self.node}:{detail}"[:200], text)
        self.store.event("ERROR", "captcha.contract_changed", detail)

    def _enqueue_solver_alert(self) -> None:
        if self._solver_alerted or not self.client.solver_engaged:
            return
        self._solver_alerted = True
        stats = self.client.solver.stats()
        text = (
            f"💳 <b>{self._label('Paid CAPTCHA solver engaged')}</b>\n"
            f"Provider: {stats['provider']}\n"
            f"Cap: {stats['max_per_hour']} solves/hour\n"
            "Passive tokens are being refused, so solves are now being purchased."
        )
        self.notifier.enqueue("captcha.solver_engaged", f"{self.node}:solver", text)
        self.store.event("WARNING", "captcha.solver_engaged", "Paid CAPTCHA solver engaged", details=stats)


    def _enqueue_recovery(self, prefix: str, recovery: dict | None) -> None:
        if not recovery:
            return
        text = (
            f"✅ <b>{self._label('Availability checker recovered')}</b>\n"
            f"Operation: {prefix}\n"
            f"Recovered after {recovery['count']} failed checks."
        )
        self.notifier.enqueue(
            "operation.recovered", str(recovery["incident_id"]), text
        )

    def _availability_alert(
        self,
        prefix: str,
        operation_id: int,
        day: str,
        slots: list[dict],
        slot_error: bool,
    ) -> None:
        earliest_slot = min(slots, key=lambda slot: slot["dateTime"]) if slots else None
        if earliest_slot:
            identity = f"{day}:{earliest_slot['id']}:{earliest_slot['dateTime']}"
            sort_key = earliest_slot["dateTime"]
        else:
            identity = f"{day}:day"
            sort_key = day
        generation = self.store.availability_transition(prefix, identity, sort_key)
        if not generation:
            return
        details = (
            f"Earliest time: {earliest_slot['dateTime']}\n"
            if earliest_slot
            else "Exact times are not available; open the official page now.\n"
        )
        if slot_error:
            details += "Slot-detail lookup failed, but the available day was verified.\n"
        text = (
            f"🚨 <b>{self._label('Appointment availability found')}</b>\n"
            f"Operation: {prefix} ({operation_id})\n"
            f"Available day: {day}\n"
            f"{details}"
            f"Book manually: {RESERVATION_PAGE_URL}"
        )
        self.notifier.enqueue(
            "availability.found", f"{prefix}:{identity}:{generation}", text
        )
        self.store.event(
            "WARNING",
            "availability.found",
            f"Availability found for {prefix} on {day}",
            operation=prefix,
            details={"operation_id": operation_id, "slot_count": len(slots)},
        )

    @staticmethod
    def _describe(exc: Exception) -> str:
        detail = str(exc).strip()
        name = type(exc).__name__
        return f"{name}: {detail}"[:200] if detail else name

    def _note_captcha_failure(self, exc: Exception) -> bool:
        """Record a CAPTCHA failure. Returns True if it counts toward backoff."""
        if isinstance(exc, CaptchaContractError):
            self._enqueue_contract_alert(str(exc))
            return True
        return isinstance(exc, CaptchaError)

    def _settle_captcha(self, any_success: bool, captcha_failed: bool) -> None:
        """Apply per-cycle backoff bookkeeping.

        Backoff is decided per cycle, not per queue: if any queue succeeded we are
        clearly not blocked, so a single flaky queue must not throttle everything.
        """
        if any_success:
            self.captcha_failure_streak = 0
            self.cooldown_until = 0.0
            return
        if not captcha_failed:
            return
        self.captcha_failure_streak += 1
        now = time.monotonic()
        if (
            self.captcha_failure_streak >= self.config.captcha_cooldown_failures
            and now >= self.cooldown_until
        ):
            self.cooldown_until = now + self.config.captcha_cooldown_seconds
            minutes = round(self.config.captcha_cooldown_seconds / 60)
            hint = (
                ""
                if self.client.solver.enabled
                else "\nNo solver configured. Set CAPTCHA_SOLVER_PROVIDER + "
                "CAPTCHA_SOLVER_API_KEY to buy solves, or run a second node on "
                "another IP."
            )
            text = (
                f"🧊 <b>{self._label('CAPTCHA cool-down engaged')}</b>\n"
                f"{self.captcha_failure_streak} consecutive blocked cycles.\n"
                f"hCaptcha is demanding interactive challenges from this IP.\n"
                f"Pausing all polling for {minutes} min to let reputation recover."
                f"{hint}\n"
                f"Check manually: {RESERVATION_PAGE_URL}"
            )
            self.notifier.enqueue(
                "captcha.cooldown", f"{self.node}:{int(self.cooldown_until)}", text
            )
            self.store.event(
                "WARNING",
                "captcha.cooldown",
                f"Cooling down for {self.config.captcha_cooldown_seconds}s after "
                f"{self.captcha_failure_streak} blocked cycles",
            )

    def _base_interval(self) -> int:
        return (
            self.config.fast_interval_seconds
            if is_within_fast_window(self.config.fast_window)
            else self.config.slow_interval_seconds
        )

    def next_interval_seconds(self) -> float:
        """Poll interval with exponential backoff while CAPTCHA keeps blocking us.

        Hammering at a fixed rate while actively blocked only deepens the
        reputation hole that caused the block.
        """
        interval = float(self._base_interval())
        if self.captcha_failure_streak:
            exponent = min(self.captcha_failure_streak, 12)
            interval = min(
                interval * (CAPTCHA_BACKOFF_MULTIPLIER ** exponent),
                float(self.config.captcha_backoff_max_seconds),
            )
        return jittered(interval, self.config.jitter_percent)

    async def run_cycle(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        try:
            await self.client.begin_cycle()
            catalog = await self.client.discover_catalog(self.config.queues)
            self.store.save_catalog(catalog)
            self.store.event(
                "INFO", "catalog.validated", "Validated and persisted operation catalog"
            )
            self._enqueue_recovery("service", self.store.resolve_incident("service"))
        except Exception as exc:
            error = type(exc).__name__
            reason = self._describe(exc)
            self._settle_captcha(False, self._note_captcha_failure(exc))
            self.store.event(
                "ERROR", "catalog.failed", f"Catalog discovery failed: {reason}"
            )
            incident = self.store.record_failure(
                "service", reason, self.config.incident_threshold, MAX_ALERT_LEVEL
            )
            self._enqueue_incident(incident)
            self.store.finish_cycle(False, error)
            try:
                await self.client.stop()
                await self.client.start()
            except Exception:
                self.store.event(
                    "ERROR", "browser.restart_failed", "Browser restart failed"
                )
            return {"service": {"status": "error", "error": error}}

        failures = 0
        successes = 0
        captcha_failed = False
        for index, prefix in enumerate(self.config.queues):
            operation = catalog[prefix]
            operation_id = operation["id"]
            if index and self.config.queue_stagger_seconds:
                # Stagger queues so A/B/C are not fired back-to-back.
                await asyncio.sleep(self.config.queue_stagger_seconds)
            self.store.record_attempt(prefix)
            try:
                await self.client.begin_queue()
                days = await self.client.get_available_days(operation_id)
                successes += 1
                self._enqueue_solver_alert()
                earliest_day = min(days) if days else None
                recovery = self.store.record_success(prefix, len(days), earliest_day)
                self._enqueue_recovery(prefix, recovery)
                slots: list[dict] = []
                slot_error = False
                if earliest_day:
                    try:
                        slots = await self.client.get_available_slots(
                            operation_id, earliest_day
                        )
                    except Exception as exc:
                        slot_error = True
                        self.store.event(
                            "WARNING",
                            "slots.failed",
                            f"Slot-detail lookup failed: {type(exc).__name__}",
                            operation=prefix,
                        )
                    self._availability_alert(
                        prefix, operation_id, earliest_day, slots, slot_error
                    )
                else:
                    self.store.availability_transition(prefix, None, None)
                result[prefix] = {
                    "status": "healthy",
                    "operation_id": operation_id,
                    "available_days": len(days),
                    "earliest_day": earliest_day,
                    "slot_count": len(slots),
                    "slot_detail_ok": not slot_error,
                }
            except Exception as exc:
                failures += 1
                error = type(exc).__name__
                reason = self._describe(exc)
                captcha_failed = self._note_captcha_failure(exc) or captcha_failed
                self.store.event(
                    "ERROR",
                    "operation.failed",
                    f"Availability check failed: {reason}",
                    operation=prefix,
                )
                incident = self.store.record_failure(
                    prefix, reason, self.config.incident_threshold, MAX_ALERT_LEVEL
                )
                self._enqueue_incident(incident)
                result[prefix] = {
                    "status": "error",
                    "operation_id": operation_id,
                    "error": error,
                    "reason": reason,
                }

        self._settle_captcha(successes > 0, captcha_failed)
        self.store.finish_cycle(failures == 0, None if failures == 0 else f"{failures} operation failures")
        self.store.event(
            "INFO" if failures == 0 else "WARNING",
            "cycle.completed",
            f"Cycle completed with {failures} operation failures",
            details={"results": result},
        )
        self.store.prune_events()
        return result

    async def _process_commands(self) -> list[int]:
        check_ids: list[int] = []
        for command in self.store.claim_commands():
            command_id = command["id"]
            kind = command["command"]
            if kind == "pause":
                self.store.set_paused(True)
                self.store.complete_command(command_id, {"paused": True})
            elif kind == "resume":
                self.store.set_paused(False)
                self.store.complete_command(command_id, {"paused": False})
            elif kind == "check":
                check_ids.append(command_id)
            else:
                self.store.complete_command(command_id, error="unsupported command")
        return check_ids

    async def run_forever(self) -> None:
        self.store.initialize()
        self.store.start_runtime()
        await self.client.start()
        outbox_task = asyncio.create_task(self.notifier.run())
        inbox_task = asyncio.create_task(self.inbox.run())
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        next_cycle = time.monotonic() + self.config.schedule_offset_seconds
        try:
            while not self._stopping:
                now = time.monotonic()
                check_ids = await self._process_commands()
                paused = bool(self.store.runtime().get("paused"))
                cooling = now < self.cooldown_until
                scheduled = not paused and not cooling and now >= next_cycle
                if scheduled or check_ids:
                    try:
                        result = await self.run_cycle()
                    except Exception as exc:
                        error = type(exc).__name__
                        logger.exception("Cycle failed")
                        for command_id in check_ids:
                            self.store.complete_command(command_id, error=error)
                    else:
                        for command_id in check_ids:
                            self.store.complete_command(command_id, result)
                    next_cycle = time.monotonic() + self.next_interval_seconds()
                    if self.cooldown_until > time.monotonic():
                        next_cycle = max(next_cycle, self.cooldown_until)
                self._maybe_daily_digest()
                await asyncio.sleep(0.5)
        finally:
            self.notifier.stop()
            self.inbox.stop()
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            await inbox_task
            await outbox_task
            await self.client.stop()

    def _maybe_daily_digest(self) -> None:
        now = datetime.now(WARSAW_TZ)
        if now.hour != self.config.daily_digest_hour:
            return
        today = now.date().isoformat()
        if self._last_digest_day == today:
            return
        if self.store.setting("digest.last_day") == today:
            self._last_digest_day = today
            return
        self._last_digest_day = today
        self.store.set_setting("digest.last_day", today)
        snapshot = self.store.snapshot()
        lines = [f"📊 <b>{self._label('Daily watcher digest')}</b>", f"Date: {today}"]
        for operation in snapshot.get("operations", []):
            lines.append(
                f"{operation['prefix']}: {operation.get('status', '?')} "
                f"a/s/f={operation.get('attempts_total', 0)}/"
                f"{operation.get('successes_total', 0)}/"
                f"{operation.get('failures_total', 0)}"
                + (
                    f" earliest={operation['earliest_day']}"
                    if operation.get("earliest_day")
                    else ""
                )
            )
        open_incidents = snapshot.get("open_incidents", [])
        lines.append(f"Open incidents: {len(open_incidents)}")
        solver = self.client.solver.stats()
        if solver["enabled"]:
            lines.append(
                f"Solver: {solver['provider']} "
                f"{solver['total_solved']}/{solver['total_calls']} solved"
            )
        self.notifier.enqueue("digest.daily", f"{self.node}:{today}", "\n".join(lines))

    async def _heartbeat_loop(self) -> None:
        while not self._stopping:
            self.store.heartbeat()
            await asyncio.sleep(5)

    def stop(self) -> None:
        self._stopping = True
