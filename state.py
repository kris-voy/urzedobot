from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from constants import EVENT_RETENTION_DAYS, EVENT_ROW_CAP, MAX_ALERT_LEVEL, STILL_DOWN_REMINDER_SECONDS

_SENSITIVE_KEY = re.compile(r"(token|cookie|captcha|secret|password|applicant|form|body)", re.I)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): ("[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class StateStore:
    def __init__(self, path: str):
        self.path = str(Path(path))

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    started_at TEXT,
                    heartbeat_at TEXT,
                    last_cycle_at TEXT,
                    last_cycle_ok INTEGER,
                    paused INTEGER NOT NULL DEFAULT 0,
                    pid INTEGER,
                    last_error TEXT
                );
                INSERT OR IGNORE INTO runtime(id, paused) VALUES(1, 0);

                CREATE TABLE IF NOT EXISTS operations (
                    prefix TEXT PRIMARY KEY,
                    operation_id INTEGER,
                    name TEXT,
                    catalog_seen_at TEXT,
                    last_attempt_at TEXT,
                    attempts_total INTEGER NOT NULL DEFAULT 0,
                    last_success_at TEXT,
                    successes_total INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'unknown',
                    available_count INTEGER,
                    earliest_day TEXT,
                    last_error TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    failures_total INTEGER NOT NULL DEFAULT 0,
                    availability_active INTEGER NOT NULL DEFAULT 0,
                    disappearance_cycles INTEGER NOT NULL DEFAULT 0,
                    last_notified_key TEXT,
                    last_notified_sort TEXT,
                    alert_generation INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS catalog_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    captured_at TEXT NOT NULL,
                    catalog_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    operation TEXT,
                    message TEXT NOT NULL,
                    details_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);

                CREATE TABLE IF NOT EXISTS incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    resolved_at TEXT,
                    failure_count INTEGER NOT NULL,
                    alerted_level INTEGER NOT NULL DEFAULT 0,
                    last_message TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_incidents_open ON incidents(scope, kind, resolved_at);

                CREATE TABLE IF NOT EXISTS commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    started_at TEXT,
                    completed_at TEXT,
                    result_json TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS telegram_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    last_error TEXT,
                    UNIQUE(kind, dedupe_key)
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_due
                    ON telegram_outbox(status, next_attempt_at);

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            self._ensure_columns(
                db,
                "operations",
                {
                    "attempts_total": "INTEGER NOT NULL DEFAULT 0",
                    "successes_total": "INTEGER NOT NULL DEFAULT 0",
                    "failures_total": "INTEGER NOT NULL DEFAULT 0",
                },
            )
            db.execute("UPDATE commands SET status='queued',started_at=NULL WHERE status='running'")

    @staticmethod
    def _ensure_columns(
        db: sqlite3.Connection, table: str, required: dict[str, str]
    ) -> None:
        existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
        for column, definition in required.items():
            if column not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def start_runtime(self) -> None:
        now = utcnow()
        with self.connect() as db:
            db.execute(
                "UPDATE runtime SET started_at=?, heartbeat_at=?, pid=?, last_error=NULL WHERE id=1",
                (now, now, os.getpid()),
            )

    def heartbeat(self) -> None:
        with self.connect() as db:
            db.execute("UPDATE runtime SET heartbeat_at=?, pid=? WHERE id=1", (utcnow(), os.getpid()))

    def set_paused(self, paused: bool) -> None:
        with self.connect() as db:
            db.execute("UPDATE runtime SET paused=? WHERE id=1", (int(paused),))
        self.event("INFO", "runtime.paused" if paused else "runtime.resumed",
                   "Watcher paused" if paused else "Watcher resumed")

    def finish_cycle(self, ok: bool, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE runtime SET last_cycle_at=?, last_cycle_ok=?, last_error=? WHERE id=1",
                (utcnow(), int(ok), error),
            )

    def runtime(self) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runtime WHERE id=1").fetchone()
        return dict(row) if row else {}

    def event(
        self,
        level: str,
        kind: str,
        message: str,
        operation: str | None = None,
        details: Any = None,
    ) -> int:
        safe_details = None if details is None else json.dumps(redact(details), ensure_ascii=False)
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO events(created_at,level,kind,operation,message,details_json) VALUES(?,?,?,?,?,?)",
                (utcnow(), level.upper(), kind, operation, message, safe_details),
            )
            return int(cursor.lastrowid)

    def events(self, after_id: int = 0, level: str | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM events WHERE id>?"
        params: list[Any] = [after_id]
        if level:
            sql += " AND level=?"
            params.append(level.upper())
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as db:
            rows = db.execute(sql, params).fetchall()
        result = [dict(row) for row in reversed(rows)]
        for row in result:
            row["details"] = json.loads(row.pop("details_json")) if row.get("details_json") else None
        return result

    def prune_events(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=EVENT_RETENTION_DAYS)).isoformat()
        with self.connect() as db:
            db.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))
            db.execute(
                "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT ?)",
                (EVENT_ROW_CAP,),
            )

    def save_catalog(self, catalog: dict[str, dict]) -> None:
        now = utcnow()
        payload = json.dumps(catalog, ensure_ascii=False, sort_keys=True)
        with self.connect() as db:
            db.execute(
                "INSERT INTO catalog_snapshots(captured_at,catalog_json) VALUES(?,?)",
                (now, payload),
            )
            for prefix, operation in catalog.items():
                db.execute(
                    """
                    INSERT INTO operations(prefix,operation_id,name,catalog_seen_at)
                    VALUES(?,?,?,?)
                    ON CONFLICT(prefix) DO UPDATE SET
                        operation_id=excluded.operation_id,
                        name=excluded.name,
                        catalog_seen_at=excluded.catalog_seen_at
                    """,
                    (prefix, operation["id"], operation["name"], now),
                )
            db.execute(
                "DELETE FROM catalog_snapshots WHERE id NOT IN "
                "(SELECT id FROM catalog_snapshots ORDER BY id DESC LIMIT 100)"
            )

    def record_attempt(self, prefix: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE operations SET last_attempt_at=?,attempts_total=attempts_total+1 WHERE prefix=?",
                (utcnow(), prefix),
            )

    def record_success(self, prefix: str, available_count: int, earliest_day: str | None) -> dict | None:
        now = utcnow()
        with self.connect() as db:
            db.execute(
                """
                UPDATE operations SET last_success_at=?,status='healthy',available_count=?,
                    earliest_day=?,last_error=NULL,consecutive_failures=0,
                    successes_total=successes_total+1
                WHERE prefix=?
                """,
                (now, available_count, earliest_day, prefix),
            )
            incident = db.execute(
                "SELECT * FROM incidents WHERE scope=? AND kind='operation.outage' "
                "AND resolved_at IS NULL ORDER BY id DESC LIMIT 1",
                (prefix,),
            ).fetchone()
            if incident:
                db.execute("UPDATE incidents SET resolved_at=?,updated_at=? WHERE id=?", (now, now, incident["id"]))
        if incident and incident["alerted_level"]:
            return {
                "kind": "operation.recovered",
                "count": incident["failure_count"],
                "incident_id": incident["id"],
            }
        return None

    def resolve_incident(self, scope: str) -> dict | None:
        now = utcnow()
        with self.connect() as db:
            incident = db.execute(
                "SELECT * FROM incidents WHERE scope=? AND kind='operation.outage' "
                "AND resolved_at IS NULL ORDER BY id DESC LIMIT 1",
                (scope,),
            ).fetchone()
            if incident:
                db.execute(
                    "UPDATE incidents SET resolved_at=?,updated_at=? WHERE id=?",
                    (now, now, incident["id"]),
                )
        if incident and incident["alerted_level"]:
            return {
                "kind": "operation.recovered",
                "count": incident["failure_count"],
                "incident_id": incident["id"],
            }
        return None

    def record_failure(
        self, scope: str, message: str, threshold: int, max_level: int = MAX_ALERT_LEVEL
    ) -> dict | None:
        now = utcnow()
        with self.connect() as db:
            if scope != "service":
                db.execute(
                    """
                    UPDATE operations SET status='error',last_error=?,
                        consecutive_failures=consecutive_failures+1,
                        failures_total=failures_total+1
                    WHERE prefix=?
                    """,
                    (message, scope),
                )
            incident = db.execute(
                "SELECT * FROM incidents WHERE scope=? AND kind='operation.outage' "
                "AND resolved_at IS NULL ORDER BY id DESC LIMIT 1",
                (scope,),
            ).fetchone()
            if incident:
                count = incident["failure_count"] + 1
                alerted_level = incident["alerted_level"]
                db.execute(
                    "UPDATE incidents SET updated_at=?,failure_count=?,last_message=? WHERE id=?",
                    (now, count, message, incident["id"]),
                )
            else:
                count = 1
                alerted_level = 0
                cursor = db.execute(
                    """
                    INSERT INTO incidents(scope,kind,opened_at,updated_at,failure_count,last_message)
                    VALUES(?,'operation.outage',?,?,1,?)
                    """,
                    (scope, now, now, message),
                )
                incident = {"id": cursor.lastrowid}
            target_level = 0
            if count >= threshold:
                target_level = 1
                while count >= threshold * (3 ** target_level):
                    target_level += 1
            # Cap escalation, then degrade to a periodic "still down" reminder so a
            # multi-hour outage cannot produce an unbounded stream of alerts.
            reminder = False
            if target_level > max_level:
                target_level = max_level
            if target_level >= max_level and alerted_level >= max_level:
                last = parse_time(self._setting(db, f"still_down:{scope}"))
                now_ts = datetime.now(timezone.utc)
                if last is None or (now_ts - last).total_seconds() >= STILL_DOWN_REMINDER_SECONDS:
                    self._set_setting(db, f"still_down:{scope}", now)
                    reminder = True
            if target_level > alerted_level or reminder:
                db.execute("UPDATE incidents SET alerted_level=? WHERE id=?", (target_level, incident["id"]))
                if target_level > alerted_level:
                    self._set_setting(db, f"still_down:{scope}", now)
                return {
                    "kind": "operation.outage",
                    "scope": scope,
                    "count": count,
                    "reason": message,
                    "escalation": target_level > 1,
                    "reminder": reminder,
                    "stamp": now,
                    "incident_id": incident["id"],
                    "level": target_level,
                }
        return None

    def availability_transition(self, prefix: str, identity: str | None, sort_key: str | None) -> int:
        with self.connect() as db:
            row = db.execute("SELECT * FROM operations WHERE prefix=?", (prefix,)).fetchone()
            if not row:
                return 0
            if identity is None:
                cycles = row["disappearance_cycles"] + 1
                active = 0 if cycles >= 2 else row["availability_active"]
                db.execute(
                    "UPDATE operations SET disappearance_cycles=?,availability_active=? WHERE prefix=?",
                    (cycles, active, prefix),
                )
                return 0
            should_alert = (
                row["last_notified_key"] is None
                or (sort_key is not None and row["last_notified_sort"] is not None
                    and sort_key < row["last_notified_sort"])
                or (not row["availability_active"] and row["disappearance_cycles"] >= 2)
            )
            values: list[Any] = [1, 0]
            sql = "UPDATE operations SET availability_active=?,disappearance_cycles=?"
            if should_alert:
                sql += ",last_notified_key=?,last_notified_sort=?,alert_generation=alert_generation+1"
                values.extend([identity, sort_key])
            sql += " WHERE prefix=?"
            values.append(prefix)
            db.execute(sql, values)
            if not should_alert:
                return 0
            generation = db.execute(
                "SELECT alert_generation FROM operations WHERE prefix=?", (prefix,)
            ).fetchone()[0]
            return int(generation)

    def enqueue_outbox(self, kind: str, dedupe_key: str, text: str) -> bool:
        now = utcnow()
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO telegram_outbox
                    (kind,dedupe_key,text,status,next_attempt_at,created_at)
                VALUES(?,?,?,'pending',?,?)
                """,
                (kind, dedupe_key, text, now, now),
            )
            return cursor.rowcount == 1

    def due_outbox(self) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT * FROM telegram_outbox
                WHERE status='pending' AND next_attempt_at<=?
                ORDER BY id LIMIT 1
                """,
                (utcnow(),),
            ).fetchone()
        return dict(row) if row else None

    def update_outbox(self, row_id: int, *, delivered: bool, attempts: int,
                      next_attempt_at: str | None = None, error: str | None = None) -> None:
        with self.connect() as db:
            if delivered:
                db.execute(
                    "UPDATE telegram_outbox SET status='delivered',attempts=?,delivered_at=?,last_error=NULL WHERE id=?",
                    (attempts, utcnow(), row_id),
                )
            elif next_attempt_at:
                db.execute(
                    "UPDATE telegram_outbox SET attempts=?,next_attempt_at=?,last_error=? WHERE id=?",
                    (attempts, next_attempt_at, error, row_id),
                )
            else:
                db.execute(
                    "UPDATE telegram_outbox SET status='undelivered',attempts=?,last_error=? WHERE id=?",
                    (attempts, error, row_id),
                )

    def enqueue_command(self, command: str) -> int:
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO commands(command,created_at,status) VALUES(?,?,'queued')",
                (command, utcnow()),
            )
            return int(cursor.lastrowid)

    def claim_commands(self) -> list[dict]:
        now = utcnow()
        with self.connect() as db:
            rows = db.execute("SELECT * FROM commands WHERE status='queued' ORDER BY id").fetchall()
            for row in rows:
                db.execute("UPDATE commands SET status='running',started_at=? WHERE id=?", (now, row["id"]))
        return [dict(row) for row in rows]

    def complete_command(self, command_id: int, result: Any = None, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE commands SET status=?,completed_at=?,result_json=?,error=? WHERE id=?",
                (
                    "failed" if error else "completed",
                    utcnow(),
                    json.dumps(redact(result), ensure_ascii=False) if result is not None else None,
                    error,
                    command_id,
                ),
            )

    def command(self, command_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
        result = dict(row) if row else None
        if result and result.get("result_json"):
            result["result"] = json.loads(result.pop("result_json"))
        return result

    def setting(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    @staticmethod
    def _setting(db: sqlite3.Connection, key: str) -> str | None:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    @staticmethod
    def _set_setting(db: sqlite3.Connection, key: str, value: str) -> None:
        db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def telegram_offset(self) -> int:
        raw = self.setting("telegram.update_offset")
        if raw is None:
            return 0
        try:
            return int(raw)
        except ValueError:
            return 0

    def set_telegram_offset(self, update_id: int) -> None:
        self.set_setting("telegram.update_offset", str(update_id))

    def snapshot(self) -> dict[str, Any]:
        with self.connect() as db:
            runtime = db.execute("SELECT * FROM runtime WHERE id=1").fetchone()
            operations = db.execute("SELECT * FROM operations ORDER BY prefix").fetchall()
            incidents = db.execute(
                "SELECT * FROM incidents WHERE resolved_at IS NULL ORDER BY opened_at"
            ).fetchall()
            undelivered = db.execute(
                "SELECT COUNT(*) FROM telegram_outbox WHERE status='undelivered'"
            ).fetchone()[0]
            pending = db.execute(
                "SELECT COUNT(*) FROM telegram_outbox WHERE status='pending'"
            ).fetchone()[0]
        return {
            "runtime": dict(runtime) if runtime else {},
            "operations": [dict(row) for row in operations],
            "open_incidents": [dict(row) for row in incidents],
            "outbox": {"pending": pending, "undelivered": undelivered},
        }
