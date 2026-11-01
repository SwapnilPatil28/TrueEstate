import json
import os
import sqlite3
from pathlib import Path
from typing import Any


DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "./data/trueestate.db"))


def connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def initialize() -> None:
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS audits (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                audit_id TEXT NOT NULL REFERENCES audits(id),
                tool TEXT NOT NULL,
                input_summary TEXT NOT NULL,
                target_url TEXT,
                run_id TEXT,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                duration_ms INTEGER,
                raw_output_json TEXT,
                is_cached INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                audit_id TEXT NOT NULL REFERENCES audits(id),
                event TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS watches (
                id TEXT PRIMARY KEY,
                audit_id TEXT NOT NULL REFERENCES audits(id),
                monitor_id TEXT,
                type TEXT NOT NULL,
                target TEXT NOT NULL,
                cron TEXT NOT NULL,
                baseline_json TEXT,
                last_checked_at TEXT,
                last_change_json TEXT
            );
            """
        )


def save_audit(audit: dict[str, Any]) -> None:
    with connect() as connection:
        connection.execute(
            "INSERT INTO audits (id, data, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
            (
                audit["id"],
                json.dumps(audit, ensure_ascii=False),
                audit["created_at"],
            ),
        )


def load_audit(audit_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT data FROM audits WHERE id = ?", (audit_id,)
        ).fetchone()
    return json.loads(row["data"]) if row else None


def add_event(audit_id: str, event: dict[str, Any]) -> int:
    with connect() as connection:
        cursor = connection.execute(
            "INSERT INTO audit_events (audit_id, event, created_at) VALUES (?, ?, ?)",
            (audit_id, json.dumps(event), event["created_at"]),
        )
        return int(cursor.lastrowid)


def events_after(audit_id: str, event_id: int) -> list[sqlite3.Row]:
    with connect() as connection:
        return connection.execute(
            "SELECT id, event FROM audit_events WHERE audit_id = ? AND id > ? ORDER BY id",
            (audit_id, event_id),
        ).fetchall()


def list_runs(audit_id: str) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM runs WHERE audit_id = ? ORDER BY started_at",
            (audit_id,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["raw_output_json"] = (
            json.loads(item["raw_output_json"]) if item["raw_output_json"] else None
        )
        item["is_cached"] = bool(item["is_cached"])
        result.append(item)
    return result


def list_watches() -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT w.*, a.data AS audit_data FROM watches w "
            "JOIN audits a ON a.id = w.audit_id ORDER BY w.last_checked_at DESC"
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["baseline_json"] = (
            json.loads(item["baseline_json"]) if item["baseline_json"] else None
        )
        item["last_change_json"] = (
            json.loads(item["last_change_json"]) if item["last_change_json"] else None
        )
        audit = json.loads(item.pop("audit_data"))
        item["project_name"] = audit.get("project_name")
        item["builder"] = audit.get("builder")
        result.append(item)
    return result
