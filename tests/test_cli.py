import json
import threading
import time
from datetime import datetime, timedelta, timezone

import svctl
from config import Config
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


def environment(monkeypatch, path):
    values = {
        "DATABASE_PATH": str(path),
        "MODE": "notify_only",
        "AUTO_CONFIRM": "false",
        "TELEGRAM_BOT_TOKEN": "token",
        "TELEGRAM_CHAT_ID": "1",
        "QUEUES": "A,B,C",
        "HEARTBEAT_STALE_SECONDS": "90",
        "CATALOG_STALE_SECONDS": "86400",
        "OPERATION_STALE_SECONDS": "900",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def healthy_store(path):
    store = StateStore(str(path))
    store.initialize()
    store.start_runtime()
    store.save_catalog(CATALOG)
    for prefix in CATALOG:
        store.record_attempt(prefix)
        store.record_success(prefix, 0, None)
    store.finish_cycle(True)
    return store


def test_status_human_and_json_output(tmp_path, monkeypatch, capsys):
    path = tmp_path / "sv.db"
    environment(monkeypatch, path)
    healthy_store(path)
    assert svctl.main(["status"]) == svctl.EXIT_OK
    assert "Status: healthy" in capsys.readouterr().out
    assert svctl.main(["status", "--json"]) == svctl.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "healthy"
    assert len(payload["operations"]) == 3


def test_paused_and_stale_heartbeat_exit_codes(tmp_path, monkeypatch):
    path = tmp_path / "sv.db"
    environment(monkeypatch, path)
    store = healthy_store(path)
    config = Config()
    store.set_paused(True)
    assert svctl.assess(store.snapshot(), config)[0] == svctl.EXIT_PAUSED
    store.set_paused(False)
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with store.connect() as db:
        db.execute("UPDATE runtime SET heartbeat_at=? WHERE id=1", (old,))
    assert svctl.assess(store.snapshot(), config)[0] == svctl.EXIT_UNAVAILABLE


def test_doctor_reports_per_operation_freshness(tmp_path, monkeypatch):
    path = tmp_path / "sv.db"
    environment(monkeypatch, path)
    store = healthy_store(path)
    code, payload = svctl.doctor_report(store)
    assert code == svctl.EXIT_OK
    assert all(item["ok"] for item in payload["checks"]["operations"].values())


def test_check_command_waits_for_running_watcher_completion(tmp_path, monkeypatch):
    path = tmp_path / "sv.db"
    environment(monkeypatch, path)
    store = healthy_store(path)

    def watcher_side():
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            commands = store.claim_commands()
            if commands:
                store.complete_command(
                    commands[0]["id"],
                    {
                        prefix: {"status": "healthy", "available_days": 0}
                        for prefix in ("A", "B", "C")
                    },
                )
                return
            time.sleep(0.02)

    worker = threading.Thread(target=watcher_side)
    worker.start()
    assert svctl.command_request(store, "check", 2, True) == svctl.EXIT_OK
    worker.join()


def test_events_json_and_level_filter(tmp_path, monkeypatch, capsys):
    path = tmp_path / "sv.db"
    environment(monkeypatch, path)
    store = healthy_store(path)
    store.event("INFO", "test.info", "info")
    store.event("ERROR", "test.error", "error")
    assert svctl.main(["events", "--level", "ERROR", "--json"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines
    assert all(json.loads(line)["level"] == "ERROR" for line in lines)

