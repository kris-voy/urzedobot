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
import logging
import sys
from datetime import datetime

from client import BezkolejkiClient
from config import Config, WARSAW_TZ
from constants import QUEUE_OPERATION_IDS, RESERVATION_PAGE_URL
from notifier import TelegramNotifier
from watcher import Watcher

# Windows consoles default to cp1252 and choke on emoji / Polish chars in our
# log + status output. Force UTF-8 on the streams (no-op where already UTF-8).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bezkolejki_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)


def _init_sentry(config: Config) -> None:
    """Opt-in error tracking. No-op (no import, no network) unless SENTRY_DSN
    is set — see README "Observability" for why this is error-tracking only,
    no performance tracing, and why local variables are excluded."""
    if not config.sentry_dsn:
        return
    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_sdk.init(
        dsn=config.sentry_dsn,
        environment=config.sentry_environment,
        # Error tracking only: this bot is a single sequential polling loop,
        # not a multi-service request flow, so performance tracing has
        # nothing useful to show and would just burn quota.
        traces_sample_rate=0.0,
        send_default_pii=False,
        # applicant.env carries one person's real PII (PESEL, passport/doc
        # numbers) which can end up as a local variable (e.g. self.config.form_data)
        # in a traceback frame — never let that leave the machine via Sentry.
        include_local_variables=False,
        # Every existing logger.error(...) call becomes a Sentry event
        # (with the preceding INFO+ log lines attached as breadcrumbs for
        # context) with no other code changes needed.
        integrations=[LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)],
    )
    logger.info("Sentry error tracking enabled (environment=%s).", config.sentry_environment)


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
    _init_sentry(config)

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
