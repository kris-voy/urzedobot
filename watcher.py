"""Main polling loop and grab (block/fill/confirm) pipeline."""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime
from typing import Optional

from client import BezkolejkiClient
from config import Config, WARSAW_TZ, is_within_fast_window, jittered
from constants import (
    MAX_CONSECUTIVE_FAILURES,
    NOTIFY_DEDUPE_SECONDS,
    POST_SUCCESS_KEEPALIVE_SECONDS,
    QUEUE_OPERATION_IDS,
    RATE_LIMIT_BACKOFF_SECONDS,
    RESERVATION_PAGE_URL,
)
from dedupe import DedupeTracker
from errors import RateLimitedError
from formfill import build_filled_properties
from notifier import TelegramNotifier

logger = logging.getLogger("bezkolejki_bot")

# Cap on the escalating backoff applied when the browser keeps failing to
# restart (e.g. a full site/network outage) — without this, run_forever would
# otherwise retry restart+cycle at the base ~30-90s cadence indefinitely.
_MAX_RESTART_BACKOFF_SECONDS = 30 * 60


class Watcher:
    def __init__(self, config: Config, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.client = BezkolejkiClient(config)
        self.notifier = TelegramNotifier(config)
        self.dedupe = DedupeTracker(NOTIFY_DEDUPE_SECONDS)
        self.consecutive_failures = 0
        self._restart_failure_streak = 0
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

    async def _restart_browser(self) -> bool:
        """Restart the browser, tracking repeated failures so a full site/
        network outage escalates the backoff instead of retrying restart+cycle
        at the base ~30-90s cadence forever. Returns True on success."""
        try:
            await self.client.restart()
            self._restart_failure_streak = 0
            return True
        except Exception as e:
            self._restart_failure_streak += 1
            backoff = min(30 * (2 ** (self._restart_failure_streak - 1)), _MAX_RESTART_BACKOFF_SECONDS)
            logger.error(
                "Browser restart failed (%d in a row): %s - backing off %ss before retrying.",
                self._restart_failure_streak, e, backoff,
            )
            await asyncio.sleep(backoff)
            return False

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
                    if not await self._restart_browser():
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
                    await self._restart_browser()
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
