"""Append-only SQLite audit log.

Every stage of the loop — cycle starts, signals, vetoes, fills, kill-switch
actions, stage errors — is recorded here as a JSON event. There are
deliberately no update or delete methods: history is immutable, which is
what makes the veto/loss log trustworthy.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class AuditLog:
    """Append-only event store backed by SQLite."""

    def __init__(self, db_path: str) -> None:
        """Create the DB (and parent dirs) if needed; create the table.

        Args:
            db_path: Filesystem path of the SQLite database.
        """
        self.db_path = str(db_path)
        parent = Path(self.db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS events(
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       ts TEXT NOT NULL,
                       event_type TEXT NOT NULL,
                       payload TEXT NOT NULL)"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)"
            )

    def record(self, event_type: str, payload: dict) -> int:
        """Append one event.

        Args:
            event_type: e.g. "signal", "veto", "fill", "cycle_start".
            payload: JSON-serializable dict.

        Returns:
            The new row id.
        """
        ts = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO events(ts, event_type, payload) VALUES (?, ?, ?)",
                (ts, event_type, json.dumps(payload, default=str)),
            )
            return int(cur.lastrowid)

    def query(self, event_type: str | None = None, limit: int = 200) -> list[dict]:
        """Read the newest events, optionally filtered by type.

        Args:
            event_type: Filter to this type, or None for all types.
            limit: Max events to return.

        Returns:
            List of ``{"id", "ts", "event_type", "payload"}`` dicts, newest
            first, with ``payload`` parsed back into a dict.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if event_type is None:
                rows = conn.execute(
                    "SELECT id, ts, event_type, payload FROM events "
                    "ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, ts, event_type, payload FROM events "
                    "WHERE event_type = ? ORDER BY id DESC LIMIT ?",
                    (event_type, limit),
                ).fetchall()
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "event_type": r["event_type"],
                "payload": json.loads(r["payload"]),
            }
            for r in rows
        ]

    def get_status(self) -> dict:
        """Return total event count and per-type counts.

        Returns:
            ``{"total_events": int, "counts": {event_type: count}}``.
        """
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT event_type, COUNT(*) FROM events GROUP BY event_type"
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return {"total_events": total, "counts": {t: c for t, c in rows}}
