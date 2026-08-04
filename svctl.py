#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import Config
from state import StateStore, parse_time

EXIT_OK = 0
EXIT_DEGRADED = 2
EXIT_PAUSED = 3
EXIT_UNAVAILABLE = 4


def _db_path() -> str:
    return os.environ.get("DATABASE_PATH", "/app/data/sv.db")


def _emit(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _age_seconds(value: str | None) -> float | None:
    parsed = parse_time(value)
    if not parsed:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def assess(snapshot: dict, config: Config) -> tuple[int, list[str]]:
    runtime = snapshot["runtime"]
    heartbeat_age = _age_seconds(runtime.get("heartbeat_at"))
    if heartbeat_age is None or heartbeat_age > config.heartbeat_stale_seconds:
        return EXIT_UNAVAILABLE, ["watcher heartbeat is unavailable or stale"]
    if runtime.get("paused"):
        return EXIT_PAUSED, ["watcher is paused"]
    reasons = []
    if runtime.get("last_cycle_ok") == 0:
        reasons.append("last cycle was not fully healthy")
    if snapshot["open_incidents"]:
        reasons.append(f"{len(snapshot['open_incidents'])} incident(s) open")
    if snapshot["outbox"]["undelivered"]:
        reasons.append(f"{snapshot['outbox']['undelivered']} alert(s) undelivered")
    for operation in snapshot["operations"]:
        catalog_age = _age_seconds(operation.get("catalog_seen_at"))
        success_age = _age_seconds(operation.get("last_success_at"))
        if catalog_age is None or catalog_age > config.catalog_stale_seconds:
            reasons.append(f"{operation['prefix']} catalog is stale")
        if success_age is None or success_age > config.operation_stale_seconds:
            reasons.append(f"{operation['prefix']} result is stale")
    if len(snapshot["operations"]) != len(config.queues):
        reasons.append("validated operation catalog is incomplete")
    return (EXIT_DEGRADED, reasons) if reasons else (EXIT_OK, [])


def status_command(store: StateStore, config: Config, as_json: bool) -> int:
    snapshot = store.snapshot()
    code, reasons = assess(snapshot, config)
    payload = {"status": {0: "healthy", 2: "degraded", 3: "paused", 4: "unavailable"}[code],
               "reasons": reasons, **snapshot}
    if as_json:
        _emit(payload, True)
    else:
        print(f"Status: {payload['status']}")
        for reason in reasons:
            print(f"  - {reason}")
        runtime = snapshot["runtime"]
        print(f"Heartbeat: {runtime.get('heartbeat_at') or 'never'}")
        print(f"Last cycle: {runtime.get('last_cycle_at') or 'never'}")
        for operation in snapshot["operations"]:
            print(
                f"{operation['prefix']}: {operation['status']}; "
                f"last success={operation.get('last_success_at') or 'never'}; "
                f"days={operation.get('available_count')}"
            )
        print(
            f"Outbox: {snapshot['outbox']['pending']} pending, "
            f"{snapshot['outbox']['undelivered']} undelivered"
        )
    return code


def doctor_report(store: StateStore) -> tuple[int, dict]:
    checks: dict[str, dict] = {}
    try:
        config = Config()
        checks["configuration"] = {"ok": True, "detail": "notify_only"}
    except ValueError as exc:
        checks["configuration"] = {"ok": False, "detail": str(exc)}
        return EXIT_UNAVAILABLE, {"status": "unavailable", "checks": checks}
    try:
        with store.connect() as db:
            db.execute("SELECT 1").fetchone()
        checks["database"] = {"ok": True, "detail": store.path}
        snapshot = store.snapshot()
    except Exception as exc:
        checks["database"] = {"ok": False, "detail": type(exc).__name__}
        return EXIT_UNAVAILABLE, {"status": "unavailable", "checks": checks}
    code, reasons = assess(snapshot, config)
    heartbeat_age = _age_seconds(snapshot["runtime"].get("heartbeat_at"))
    checks["heartbeat"] = {
        "ok": heartbeat_age is not None and heartbeat_age <= config.heartbeat_stale_seconds,
        "age_seconds": heartbeat_age,
    }
    operations = snapshot["operations"]
    checks["catalog"] = {
        "ok": len(operations) == len(config.queues)
        and all((_age_seconds(row.get("catalog_seen_at"))
                 if _age_seconds(row.get("catalog_seen_at")) is not None else float("inf"))
                <= config.catalog_stale_seconds
                for row in operations),
        "operations": len(operations),
    }
    checks["operations"] = {
        row["prefix"]: {
            "ok": (
                _age_seconds(row.get("last_success_at"))
                if _age_seconds(row.get("last_success_at")) is not None
                else float("inf")
            ) <= config.operation_stale_seconds,
            "status": row["status"],
        }
        for row in operations
    }
    return code, {
        "status": {0: "healthy", 2: "degraded", 3: "paused", 4: "unavailable"}[code],
        "reasons": reasons,
        "checks": checks,
    }


def command_request(store: StateStore, command: str, wait_seconds: float, as_json: bool) -> int:
    try:
        config = Config()
        snapshot = store.snapshot()
    except Exception as exc:
        payload = {"status": "unavailable", "error": type(exc).__name__}
        _emit(payload, as_json)
        if not as_json:
            print(f"Watcher unavailable: {payload['error']}")
        return EXIT_UNAVAILABLE
    heartbeat_age = _age_seconds(snapshot["runtime"].get("heartbeat_at"))
    if heartbeat_age is None or heartbeat_age > config.heartbeat_stale_seconds:
        payload = {"status": "unavailable", "error": "stale heartbeat"}
        _emit(payload, as_json)
        if not as_json:
            print("Watcher unavailable: stale heartbeat")
        return EXIT_UNAVAILABLE
    command_id = store.enqueue_command(command)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        row = store.command(command_id)
        if row and row["status"] in {"completed", "failed"}:
            payload = row
            _emit(payload, as_json)
            if not as_json:
                print(f"{command} command {row['status']} (id={command_id})")
                if row.get("result"):
                    print(json.dumps(row["result"], ensure_ascii=False, indent=2))
            if row["status"] == "failed":
                return EXIT_UNAVAILABLE
            if command == "pause":
                return EXIT_PAUSED
            if command == "check" and any(
                isinstance(value, dict) and value.get("status") == "error"
                for value in row.get("result", {}).values()
            ):
                return EXIT_DEGRADED
            return EXIT_OK
        time.sleep(0.2)
    payload = {"status": "timeout", "command_id": command_id}
    _emit(payload, as_json)
    if not as_json:
        print(f"{command} command timed out (id={command_id})")
    return EXIT_UNAVAILABLE


def events_command(store: StateStore, args: argparse.Namespace) -> int:
    after_id = 0
    try:
        while True:
            rows = store.events(after_id=after_id, level=args.level, limit=200)
            for row in rows:
                after_id = max(after_id, row["id"])
                if args.json:
                    print(json.dumps(row, ensure_ascii=False, sort_keys=True))
                else:
                    operation = f" [{row['operation']}]" if row["operation"] else ""
                    print(
                        f"{row['created_at']} {row['level']} {row['kind']}"
                        f"{operation}: {row['message']}"
                    )
            if not args.follow:
                return EXIT_OK
            time.sleep(1)
    except KeyboardInterrupt:
        return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="svctl", description="Notify-only watcher administration")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "doctor"):
        item = sub.add_parser(name)
        item.add_argument("--json", action="store_true")
    events = sub.add_parser("events")
    events.add_argument("--follow", action="store_true")
    events.add_argument("--level")
    events.add_argument("--json", action="store_true")
    check = sub.add_parser("check")
    check.add_argument("--wait", type=float, default=120)
    check.add_argument("--json", action="store_true")
    for name in ("pause", "resume"):
        item = sub.add_parser(name)
        item.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = _db_path()
    if not Path(path).exists():
        payload = {"status": "unavailable", "error": "database does not exist"}
        _emit(payload, getattr(args, "json", False))
        if not getattr(args, "json", False):
            print("Watcher unavailable: database does not exist")
        return EXIT_UNAVAILABLE
    store = StateStore(path)
    if args.command == "events":
        return events_command(store, args)
    if args.command == "doctor":
        code, payload = doctor_report(store)
        _emit(payload, args.json)
        if not args.json:
            print(f"Doctor: {payload['status']}")
            for name, check in payload["checks"].items():
                print(f"  {name}: {'OK' if check.get('ok', all(v.get('ok') for v in check.values()) if check else False) else 'FAIL'}")
            for reason in payload.get("reasons", []):
                print(f"  - {reason}")
        return code
    config = Config()
    if args.command == "status":
        return status_command(store, config, args.json)
    wait = args.wait if args.command == "check" else 10
    return command_request(store, args.command, wait, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
