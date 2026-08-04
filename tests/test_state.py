from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from state import StateStore


CATALOG = {
    prefix: {
        "id": operation_id,
        "prefix": prefix,
        "name": f"Wydawanie dokumentów (karty pobytu, zaproszenia) {prefix}",
        "isReservationActive": True,
    }
    for prefix, operation_id in {"A": 3213864, "B": 3219596, "C": 3219597}.items()
}


def store(tmp_path):
    value = StateStore(str(tmp_path / "sv.db"))
    value.initialize()
    return value


def test_wal_busy_timeout_and_concurrent_writes(tmp_path):
    value = store(tmp_path)
    with value.connect() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda number: value.event("INFO", "test", f"event {number}"), range(100)))
    assert len(value.events(limit=200)) == 100


def test_events_redact_sensitive_structured_fields(tmp_path):
    value = store(tmp_path)
    value.event(
        "ERROR",
        "test",
        "safe summary",
        details={"token": "secret", "nested": {"raw_body": "private"}, "count": 1},
    )
    event = value.events()[0]
    assert event["details"]["token"] == "[REDACTED]"
    assert event["details"]["nested"]["raw_body"] == "[REDACTED]"
    assert event["details"]["count"] == 1


def test_availability_dedup_and_return_after_two_empty_cycles(tmp_path):
    value = store(tmp_path)
    value.save_catalog(CATALOG)
    assert value.availability_transition("A", "day:slot", "2026-07-18T08:00") == 1
    assert value.availability_transition("A", "day:slot", "2026-07-18T08:00") == 0
    value.availability_transition("A", None, None)
    assert value.availability_transition("A", "day:slot", "2026-07-18T08:00") == 0
    value.availability_transition("A", None, None)
    value.availability_transition("A", None, None)
    assert value.availability_transition("A", "day:slot", "2026-07-18T08:00") == 2


def test_new_earlier_availability_realerts(tmp_path):
    value = store(tmp_path)
    value.save_catalog(CATALOG)
    assert value.availability_transition("A", "later", "2026-07-20") == 1
    assert value.availability_transition("A", "earlier", "2026-07-18") == 2


def test_incident_opens_escalates_and_resolves(tmp_path):
    value = store(tmp_path)
    value.save_catalog(CATALOG)
    assert value.record_failure("A", "timeout", 3) is None
    assert value.record_failure("A", "timeout", 3) is None
    opened = value.record_failure("A", "timeout", 3)
    assert opened["count"] == 3
    assert opened["reason"] == "timeout"
    for _ in range(5):
        assert value.record_failure("A", "timeout", 3) is None
    escalated = value.record_failure("A", "timeout", 3)
    assert escalated["count"] == 9
    assert escalated["escalation"]
    recovered = value.record_success("A", 0, None)
    assert recovered["count"] == 9
    assert value.snapshot()["open_incidents"] == []


def test_restart_requeues_in_progress_commands(tmp_path):
    value = store(tmp_path)
    command_id = value.enqueue_command("check")
    assert value.claim_commands()[0]["id"] == command_id
    assert value.command(command_id)["status"] == "running"
    value.initialize()
    assert value.command(command_id)["status"] == "queued"


def test_stale_timestamp_fixture_for_cli(tmp_path):
    value = store(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with value.connect() as db:
        db.execute("UPDATE runtime SET heartbeat_at=? WHERE id=1", (old,))
    assert value.runtime()["heartbeat_at"] == old


def test_operation_counters_and_telegram_offset_persist(tmp_path):
    value = store(tmp_path)
    value.save_catalog(CATALOG)
    value.record_attempt("A")
    value.record_success("A", 0, None)
    value.record_failure("A", "timeout", 3)
    operation = next(row for row in value.snapshot()["operations"] if row["prefix"] == "A")
    assert operation["attempts_total"] == 1
    assert operation["successes_total"] == 1
    assert operation["failures_total"] == 1
    assert value.telegram_offset() == 0
    value.set_telegram_offset(42)
    assert value.telegram_offset() == 42
