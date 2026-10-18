"""SQLite store. Every table is keyed by user_id so one database can hold
many users once this runs hosted; locally there is just one."""

import json
import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from whoop_engine.normalize import MetricRow, Normalized, RecordState

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_records (
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    id TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (user_id, kind, id)
);
CREATE TABLE IF NOT EXISTS daily_metrics (
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    source_kind TEXT NOT NULL,
    source_ids TEXT NOT NULL,
    complete INTEGER NOT NULL,
    PRIMARY KEY (user_id, metric, day)
);
CREATE TABLE IF NOT EXISTS record_states (
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    id TEXT NOT NULL,
    day TEXT NOT NULL,
    score_state TEXT NOT NULL,
    role TEXT NOT NULL,
    day_source TEXT NOT NULL,
    PRIMARY KEY (user_id, kind, id)
);
CREATE TABLE IF NOT EXISTS meta (
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    label TEXT NOT NULL,
    value REAL NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    spec TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record_id(kind: str, record: dict) -> str:
    return str(record["cycle_id"] if kind == "recovery" else record["id"])


class Store:
    def __init__(self, path):
        if str(path) != ":memory:":
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Create the file owner-only before SQLite opens it: it holds health data.
            os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
            path.chmod(0o600)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # --- raw WHOOP records ------------------------------------------------

    def upsert_raw(self, user_id: int, kind: str, records) -> None:
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO raw_records VALUES (?, ?, ?, ?)",
                [(user_id, kind, _record_id(kind, r), json.dumps(r)) for r in records],
            )

    def delete_raw(self, user_id: int, kind: str, ids) -> None:
        with self.db:
            self.db.executemany(
                "DELETE FROM raw_records WHERE user_id = ? AND kind = ? AND id = ?",
                [(user_id, kind, str(i)) for i in ids],
            )

    def raw(self, user_id: int, kind: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT payload FROM raw_records WHERE user_id = ? AND kind = ? ORDER BY rowid",
            (user_id, kind),
        )
        return [json.loads(r["payload"]) for r in rows]

    # --- derived daily metrics -------------------------------------------

    def replace_derived(self, user_id: int, normalized: Normalized) -> None:
        with self.db:
            self.db.execute("DELETE FROM daily_metrics WHERE user_id = ?", (user_id,))
            self.db.execute("DELETE FROM record_states WHERE user_id = ?", (user_id,))
            self.db.executemany(
                "INSERT INTO daily_metrics VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (user_id, m.day.isoformat(), m.metric, m.value, m.source_kind,
                     json.dumps(list(m.source_ids)), int(m.complete))
                    for m in normalized.metrics
                ],
            )
            self.db.executemany(
                "INSERT OR REPLACE INTO record_states VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (user_id, s.kind, s.id, s.day.isoformat(), s.score_state, s.role, s.day_source)
                    for s in normalized.records
                ],
            )

    def metric_rows(self, user_id: int, metric: str, start: date, end: date) -> list[MetricRow]:
        rows = self.db.execute(
            "SELECT * FROM daily_metrics WHERE user_id = ? AND metric = ? "
            "AND day BETWEEN ? AND ? ORDER BY day",
            (user_id, metric, start.isoformat(), end.isoformat()),
        )
        return [
            MetricRow(date.fromisoformat(r["day"]), r["metric"], r["value"], r["source_kind"],
                      tuple(json.loads(r["source_ids"])), bool(r["complete"]))
            for r in rows
        ]

    def record_states(self, user_id: int, start: date, end: date) -> list[RecordState]:
        rows = self.db.execute(
            "SELECT * FROM record_states WHERE user_id = ? AND day BETWEEN ? AND ? "
            "ORDER BY day, kind, id",
            (user_id, start.isoformat(), end.isoformat()),
        )
        return [
            RecordState(r["kind"], r["id"], date.fromisoformat(r["day"]), r["score_state"],
                        r["role"], r["day_source"])
            for r in rows
        ]

    # --- per-user metadata (sync state) ----------------------------------

    def get_meta(self, user_id: int, key: str) -> str | None:
        row = self.db.execute(
            "SELECT value FROM meta WHERE user_id = ? AND key = ?", (user_id, key)
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, user_id: int, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?, ?)", (user_id, key, value))

    # --- life events (user-logged annotations) ---------------------------

    def add_event(self, user_id: int, day: date, label: str, value: float = 1, note=None) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO events (user_id, day, label, value, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, day.isoformat(), label, value, note, _now()),
            )
        return cur.lastrowid

    def events(self, user_id: int, start: date, end: date, label: str | None = None) -> list[dict]:
        sql = "SELECT id, day, label, value, note FROM events WHERE user_id = ? AND day BETWEEN ? AND ?"
        args: list = [user_id, start.isoformat(), end.isoformat()]
        if label is not None:
            sql += " AND label = ?"
            args.append(label)
        return [dict(r) for r in self.db.execute(sql + " ORDER BY day, id", args)]

    def delete_event(self, user_id: int, event_id: int) -> bool:
        with self.db:
            cur = self.db.execute(
                "DELETE FROM events WHERE user_id = ? AND id = ?", (user_id, event_id)
            )
        return cur.rowcount > 0

    # --- goals ------------------------------------------------------------

    def add_goal(self, user_id: int, spec: dict) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO goals (user_id, spec, created_at) VALUES (?, ?, ?)",
                (user_id, json.dumps(spec), _now()),
            )
        return cur.lastrowid

    def goals(self, user_id: int, include_archived: bool = False) -> list[dict]:
        sql = "SELECT id, spec, archived FROM goals WHERE user_id = ?"
        if not include_archived:
            sql += " AND archived = 0"
        return [
            {**json.loads(r["spec"]), "id": r["id"], "archived": bool(r["archived"])}
            for r in self.db.execute(sql + " ORDER BY id", (user_id,))
        ]

    def archive_goal(self, user_id: int, goal_id: int) -> bool:
        with self.db:
            cur = self.db.execute(
                "UPDATE goals SET archived = 1 WHERE user_id = ? AND id = ? AND archived = 0",
                (user_id, goal_id),
            )
        return cur.rowcount > 0
