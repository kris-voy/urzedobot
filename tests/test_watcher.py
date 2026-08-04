import asyncio
from pathlib import Path

from config import Config
from constants import MAX_ALERT_LEVEL
from errors import CaptchaChallengeError, CaptchaContractError
from notifier import TelegramOutbox
from state import StateStore
from watcher import Watcher


CATALOG = {
    prefix: {
        "id": operation_id,
        "prefix": prefix,
        "name": f"Wydawanie dokumentów (karty pobytu, zaproszenia) {prefix}",
        "isReservationActive": True,
    }
    for prefix, operation_id in {"A": 3213864, "B": 3219596, "C": 3219597}.items()
}


class FakeSolver:
    def __init__(self, enabled=False):
        self.enabled = enabled

    def stats(self):
        return {
            "provider": "none",
            "enabled": self.enabled,
            "calls_last_hour": 0,
            "max_per_hour": 60,
            "total_calls": 0,
            "total_solved": 0,
            "total_failed": 0,
        }


class FakeClient:
    def __init__(self, days=None, slots=None, fail_slots=False):
        self.days = days or {value["id"]: [] for value in CATALOG.values()}
        self.slots = slots or []
        self.fail_slots = fail_slots
        self.calls = []
        self.solver = FakeSolver()
        self.solver_engaged = False
        self.last_challenge_reason = None

    async def start(self):
        self.calls.append("start")

    async def stop(self):
        self.calls.append("stop")

    async def begin_cycle(self):
        self.calls.append("begin_cycle")

    async def discover_catalog(self, prefixes):
        self.calls.append("discover_catalog")
        return {prefix: CATALOG[prefix] for prefix in prefixes}

    async def begin_queue(self):
        self.calls.append("begin_queue")

    async def get_available_days(self, operation_id):
        self.calls.append(("days", operation_id))
        value = self.days[operation_id]
        if isinstance(value, Exception):
            raise value
        return value

    async def get_available_slots(self, operation_id, day):
        self.calls.append(("slots", operation_id, day))
        if self.fail_slots:
            raise TimeoutError()
        return self.slots


def setup(tmp_path, client, **overrides):
    store = StateStore(str(tmp_path / "sv.db"))
    store.initialize()
    options = dict(
        mode="notify_only",
        auto_confirm=False,
        telegram_bot_token="token",
        telegram_chat_id="1",
        queues=["A", "B", "C"],
        database_path=store.path,
        incident_threshold=2,
        queue_stagger_seconds=0,
    )
    options.update(overrides)
    config = Config(**options)
    notifier = TelegramOutbox(config, store, sender=lambda _: True)
    return store, Watcher(config, store=store, client=client, notifier=notifier)


def outbox_rows(store):
    with store.connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM telegram_outbox ORDER BY id")]


def test_partial_abc_failure_preserves_per_operation_freshness(tmp_path):
    days = {value["id"]: [] for value in CATALOG.values()}
    days[3219596] = TimeoutError()
    store, watcher = setup(tmp_path, FakeClient(days=days))
    result = asyncio.run(watcher.run_cycle())
    assert result["A"]["status"] == "healthy"
    assert result["B"]["status"] == "error"
    assert result["C"]["status"] == "healthy"
    operations = {row["prefix"]: row for row in store.snapshot()["operations"]}
    assert operations["A"]["last_success_at"]
    assert operations["B"]["last_success_at"] is None
    assert operations["C"]["last_success_at"]


def test_availability_sends_one_alert_and_empty_cycles_send_nothing(tmp_path):
    days = {value["id"]: [] for value in CATALOG.values()}
    days[3213864] = ["2026-07-18T00:00:00+02:00"]
    slots = [{"id": 10, "dateTime": "2026-07-18T08:00:00+02:00"}]
    client = FakeClient(days=days, slots=slots)
    store, watcher = setup(tmp_path, client)
    asyncio.run(watcher.run_cycle())
    asyncio.run(watcher.run_cycle())
    assert len(outbox_rows(store)) == 1
    client.days[3213864] = []
    asyncio.run(watcher.run_cycle())
    asyncio.run(watcher.run_cycle())
    assert len(outbox_rows(store)) == 1
    client.days[3213864] = ["2026-07-18T00:00:00+02:00"]
    asyncio.run(watcher.run_cycle())
    assert len(outbox_rows(store)) == 2


def test_day_alert_survives_slot_detail_failure(tmp_path):
    days = {value["id"]: [] for value in CATALOG.values()}
    days[3213864] = ["2026-07-18T00:00:00+02:00"]
    store, watcher = setup(tmp_path, FakeClient(days=days, fail_slots=True))
    result = asyncio.run(watcher.run_cycle())
    assert result["A"]["status"] == "healthy"
    assert not result["A"]["slot_detail_ok"]
    rows = outbox_rows(store)
    assert len(rows) == 1
    assert "Slot-detail lookup failed" in rows[0]["text"]
    assert "https://uw.bezkolejki.eu/ouw/Reservation" in rows[0]["text"]


def test_outage_alert_includes_failure_reason(tmp_path):
    days = {value["id"]: [] for value in CATALOG.values()}
    days[3219596] = TimeoutError()
    store, watcher = setup(tmp_path, FakeClient(days=days))
    asyncio.run(watcher.run_cycle())
    asyncio.run(watcher.run_cycle())
    rows = outbox_rows(store)
    outage = [row for row in rows if row["kind"] == "operation.outage"]
    assert outage
    assert "Reason: TimeoutError" in outage[0]["text"]


def test_pause_resume_and_check_commands_are_claimed_by_watcher(tmp_path):
    store, watcher = setup(tmp_path, FakeClient())
    pause_id = store.enqueue_command("pause")
    assert asyncio.run(watcher._process_commands()) == []
    assert store.runtime()["paused"] == 1
    assert store.command(pause_id)["status"] == "completed"
    check_id = store.enqueue_command("check")
    assert asyncio.run(watcher._process_commands()) == [check_id]
    resume_id = store.enqueue_command("resume")
    assert asyncio.run(watcher._process_commands()) == []
    assert store.runtime()["paused"] == 0
    assert store.command(resume_id)["status"] == "completed"


def test_active_runtime_has_no_mutation_endpoints():
    root = Path(__file__).parents[1]
    active = "\n".join(
        (root / name).read_text(encoding="utf-8")
        for name in ("client.py", "watcher.py", "bot.py")
    )
    for forbidden in ("BlockSlot", "UpdateSlotProperties", "ConfirmReservation"):
        assert forbidden not in active
    assert "method: 'GET'" in active


def test_node_name_labels_alerts(tmp_path):
    days = {value["id"]: [] for value in CATALOG.values()}
    days[3213864] = ["2026-07-18T00:00:00+02:00"]
    store, watcher = setup(tmp_path, FakeClient(days=days), node_name="rpi-home2")
    asyncio.run(watcher.run_cycle())
    rows = outbox_rows(store)
    assert rows
    assert "[rpi-home2]" in rows[0]["text"]


def test_captcha_contract_change_sends_distinct_alert_once(tmp_path):
    days = {value["id"]: [] for value in CATALOG.values()}
    days[3219596] = CaptchaContractError("page exposes no usable hcaptcha API")
    store, watcher = setup(tmp_path, FakeClient(days=days))
    asyncio.run(watcher.run_cycle())
    asyncio.run(watcher.run_cycle())
    contract = [r for r in outbox_rows(store) if r["kind"] == "captcha.contract_changed"]
    assert len(contract) == 1
    assert "watcher is blind" in contract[0]["text"]


def test_captcha_failures_trigger_exponential_backoff(tmp_path):
    days = {value["id"]: CaptchaChallengeError("challenge-expired") for value in CATALOG.values()}
    store, watcher = setup(tmp_path, FakeClient(days=days), jitter_percent=0.0, slow_interval_seconds=60, fast_interval_seconds=60)
    base = watcher.next_interval_seconds()
    asyncio.run(watcher.run_cycle())
    assert watcher.captcha_failure_streak == 1
    first = watcher.next_interval_seconds()
    assert first > base
    asyncio.run(watcher.run_cycle())
    assert watcher.next_interval_seconds() > first


def test_backoff_is_capped_and_resets_on_success(tmp_path):
    days = {value["id"]: CaptchaChallengeError("challenge-expired") for value in CATALOG.values()}
    client = FakeClient(days=days)
    store, watcher = setup(
        tmp_path, client, jitter_percent=0.0, slow_interval_seconds=60, fast_interval_seconds=60, captcha_backoff_max_seconds=600
    )
    for _ in range(12):
        asyncio.run(watcher.run_cycle())
    assert watcher.next_interval_seconds() <= 600
    client.days[3219596] = []
    asyncio.run(watcher.run_cycle())
    assert watcher.captcha_failure_streak == 0


def test_cooldown_engages_after_repeated_captcha_failures(tmp_path):
    days = {value["id"]: CaptchaChallengeError("challenge-expired") for value in CATALOG.values()}
    store, watcher = setup(tmp_path, FakeClient(days=days), captcha_cooldown_failures=3)
    for _ in range(3):
        asyncio.run(watcher.run_cycle())
    assert watcher.cooldown_until > 0
    cooldown = [r for r in outbox_rows(store) if r["kind"] == "captcha.cooldown"]
    assert len(cooldown) == 1
    assert "Pausing all polling" in cooldown[0]["text"]


def test_outage_escalation_is_capped(tmp_path):
    days = {value["id"]: [] for value in CATALOG.values()}
    days[3219596] = TimeoutError()
    store, watcher = setup(tmp_path, FakeClient(days=days))
    for _ in range(200):
        asyncio.run(watcher.run_cycle())
    levels = [
        int(r["dedupe_key"].split(":")[1])
        for r in outbox_rows(store)
        if r["kind"] == "operation.outage"
    ]
    assert levels
    assert max(levels) <= MAX_ALERT_LEVEL
