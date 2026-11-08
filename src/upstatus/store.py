"""The history: one SQLite file with the results, the state of every check, the incidents and the pending events."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from .model import Result
from .tracker import Event, Incident

SCHEMA = """
CREATE TABLE IF NOT EXISTS results (check_name TEXT NOT NULL, ts REAL NOT NULL, ok INTEGER NOT NULL, ms REAL, detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS results_by_check ON results (check_name, ts);
CREATE TABLE IF NOT EXISTS state (check_name TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS incidents (id INTEGER PRIMARY KEY, check_name TEXT NOT NULL, kind TEXT NOT NULL, start REAL NOT NULL, "end" REAL, detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS incidents_by_check ON incidents (check_name, start);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, check_name TEXT NOT NULL, kind TEXT NOT NULL, ts REAL NOT NULL, since REAL NOT NULL, detail TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0);
PRAGMA user_version = 1;
"""
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PendingEvent:
    id: int
    check: str
    kind: str
    ts: float
    since: float
    detail: str


class StoreError(Exception):
    """The history cannot be used (wrong file, newer version)."""


class Store:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "upstatus.db"
        try:
            self.conn = sqlite3.connect(self.path, timeout=30)
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise StoreError(f"{self.path} was written by a newer version of upstatus")
            self.conn.executescript(SCHEMA)
        except sqlite3.DatabaseError as exc:
            self.close()
            raise StoreError(f"{self.path} is not a usable history: {exc}") from None
        except StoreError:
            self.close()
            raise

    def close(self) -> None:
        conn = getattr(self, "conn", None)
        if conn is not None:
            conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.conn:
            yield self.conn

    # results -----------------------------------------------------------------------------------------------------
    def add_results(self, results: list[Result]) -> None:
        with self.transaction() as c:
            c.executemany(
                "INSERT INTO results VALUES (?, ?, ?, ?, ?)",
                [(r.check, r.epoch, int(r.ok), r.ms, r.detail) for r in results],
            )

    def results(self, check: str, since: float, until: float = 1e18) -> list[tuple[float, bool, float | None]]:
        rows = self.conn.execute(
            "SELECT ts, ok, ms FROM results WHERE check_name = ? AND ts >= ? AND ts <= ? ORDER BY ts",
            (check, since, until),
        ).fetchall()
        return [(ts, bool(ok), ms) for ts, ok, ms in rows]

    def count_results(self, check: str | None = None) -> int:
        if check is None:
            return int(self.conn.execute("SELECT COUNT(*) FROM results").fetchone()[0])
        return int(self.conn.execute("SELECT COUNT(*) FROM results WHERE check_name = ?", (check,)).fetchone()[0])

    def first_seen(self, check: str) -> float | None:
        row = self.conn.execute("SELECT MIN(ts) FROM results WHERE check_name = ?", (check,)).fetchone()
        return None if row[0] is None else float(row[0])

    def last_detail(self, check: str) -> str:
        row = self.conn.execute(
            "SELECT detail FROM results WHERE check_name = ? ORDER BY ts DESC LIMIT 1", (check,)
        ).fetchone()
        return "" if row is None else str(row[0])

    def prune(self, before: float) -> int:
        with self.transaction() as c:
            n = c.execute("DELETE FROM results WHERE ts < ?", (before,)).rowcount
            c.execute('DELETE FROM incidents WHERE "end" IS NOT NULL AND "end" < ?', (before,))
            c.execute("DELETE FROM events WHERE sent = 1 AND ts < ?", (before,))
        return int(n)

    # state -------------------------------------------------------------------------------------------------------
    def load_state(self, check: str) -> dict[str, object]:
        row = self.conn.execute("SELECT body FROM state WHERE check_name = ?", (check,)).fetchone()
        if row is None:
            return {}
        try:
            data = json.loads(row[0])
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def save_state(self, check: str, body: dict[str, object]) -> None:
        with self.transaction() as c:
            c.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (check, json.dumps(body, separators=(",", ":"))))

    def forget(self, keep: list[str]) -> None:
        """Drop the state of checks that are no longer in the configuration (their history stays)."""
        with self.transaction() as c:
            names = [r[0] for r in c.execute("SELECT check_name FROM state")]
            for name in names:
                if name not in keep:
                    c.execute("DELETE FROM state WHERE check_name = ?", (name,))

    # events and incidents ----------------------------------------------------------------------------------------
    def apply_events(self, check: str, events: list[Event]) -> None:
        """Record the events (to be sent) and keep the incident table in step with them."""
        if not events:
            return
        with self.transaction() as c:
            for ev in events:
                c.execute(
                    "INSERT INTO events (check_name, kind, ts, since, detail) VALUES (?, ?, ?, ?, ?)",
                    (check, ev.kind, ev.ts, ev.since, ev.detail),
                )
                if ev.kind == "down":
                    c.execute(
                        'UPDATE incidents SET "end" = ? WHERE check_name = ? AND kind = \'degraded\' AND "end" IS NULL',
                        (ev.since, check),
                    )
                    c.execute(
                        "INSERT INTO incidents (check_name, kind, start, detail) VALUES (?, 'down', ?, ?)",
                        (check, ev.since, ev.detail),
                    )
                elif ev.kind == "up":
                    c.execute(
                        'UPDATE incidents SET "end" = ? WHERE check_name = ? AND kind = \'down\' AND "end" IS NULL',
                        (ev.since, check),
                    )
                elif ev.kind == "degraded":
                    c.execute(
                        "INSERT INTO incidents (check_name, kind, start, detail) VALUES (?, 'degraded', ?, ?)",
                        (check, ev.since, ev.detail),
                    )
                elif ev.kind == "normal":
                    c.execute(
                        'UPDATE incidents SET "end" = ? WHERE check_name = ? AND kind = \'degraded\' AND "end" IS NULL',
                        (ev.since, check),
                    )

    def incidents(self, check: str | None = None, since: float = 0.0) -> list[tuple[str, Incident]]:
        """Incidents that ended after ``since`` or are still open, as (check, incident)."""
        sql = 'SELECT check_name, kind, start, "end", detail FROM incidents WHERE (("end" IS NULL) OR "end" >= ?)'
        args: list[object] = [since]
        if check is not None:
            sql += " AND check_name = ?"
            args.append(check)
        rows = self.conn.execute(sql + " ORDER BY start", args).fetchall()
        return [(name, Incident(kind, start, end, detail)) for name, kind, start, end, detail in rows]

    def pending(self) -> list[PendingEvent]:
        rows = self.conn.execute(
            "SELECT id, check_name, kind, ts, since, detail FROM events WHERE sent = 0 ORDER BY id"
        ).fetchall()
        return [PendingEvent(*row) for row in rows]

    def mark_sent(self, event_id: int) -> None:
        with self.transaction() as c:
            c.execute("UPDATE events SET sent = 1 WHERE id = ?", (event_id,))

    def mark_all_sent(self) -> None:
        with self.transaction() as c:
            c.execute("UPDATE events SET sent = 1")
