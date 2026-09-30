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

    # ---- read-only conveniences (additive; nothing here mutates history) ----

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> dict:
        """Map a SQLite row to the public event dict shape."""
        return {
            "id": row["id"],
            "ts": row["ts"],
            "event_type": row["event_type"],
            "payload": json.loads(row["payload"]),
        }

    def event_by_id(self, event_id: int) -> dict | None:
        """Fetch one event by row id; None when absent.

        Args:
            event_id: The ``events.id`` value.

        Returns:
            Event dict or None.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, ts, event_type, payload FROM events WHERE id = ?",
                (int(event_id),),
            ).fetchone()
        return self._event_from_row(row) if row is not None else None

    def query_types(self, event_types: list[str], limit: int = 500) -> list[dict]:
        """Newest events whose type is in ``event_types``.

        Args:
            event_types: Event type names (e.g. ["scan", "signal"]).
            limit: Max events to return.

        Returns:
            Event dicts, newest first.
        """
        types = [str(t) for t in event_types if str(t)]
        if not types:
            return []
        placeholders = ",".join("?" for _ in types)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT id, ts, event_type, payload FROM events "
                f"WHERE event_type IN ({placeholders}) ORDER BY id DESC LIMIT ?",
                (*types, int(limit)),
            ).fetchall()
        return [self._event_from_row(r) for r in rows]

    def signal_by_ref(self, ref: str) -> dict | None:
        """Find a stored ``signal`` event by canonical ref.

        A ref is either a payload ``signal_id`` (uuid hex written by the
        extension scan) or an audit row reference for loop-written signals:
        ``"<id>"`` or ``"row:<id>"``.

        Args:
            ref: Signal id or row reference.

        Returns:
            The signal event dict, or None when no signal matches.
        """
        raw = str(ref or "").strip()
        if not raw:
            return None
        row_id: int | None = None
        if raw.lower().startswith("row:"):
            tail = raw[4:]
            if tail.isdigit():
                row_id = int(tail)
        elif raw.isdigit():
            row_id = int(raw)
        if row_id is not None:
            event = self.event_by_id(row_id)
            if event is not None and event["event_type"] == "signal":
                return event
            return None
        for event in self.query("signal", limit=5000):
            payload = event.get("payload") or {}
            if str(payload.get("signal_id") or "") == raw:
                return event
        return None

    def previous_validation(self, ref: str) -> dict | None:
        """Newest ``validation`` event for a canonical signal ref, if any.

        Args:
            ref: Canonical signal ref stored in validation payloads.

        Returns:
            The validation event dict or None.
        """
        wanted = str(ref or "")
        if not wanted:
            return None
        for event in self.query("validation", limit=5000):
            payload = event.get("payload") or {}
            if str(payload.get("signal_ref") or "") == wanted:
                return event
        return None

    def validations(self, limit: int = 5000) -> list[dict]:
        """All ``validation`` events, newest first."""
        return self.query("validation", limit=limit)

    def predictions(self, limit: int = 500) -> list[dict]:
        """All ``predict`` (market-guesser) events, newest first."""
        return self.query("predict", limit=limit)

    def prediction_resolutions(self, limit: int = 500) -> list[dict]:
        """All ``predict_resolved`` events, newest first."""
        return self.query("predict_resolved", limit=limit)

    def prediction_by_id(self, prediction_id: str) -> dict | None:
        """Find one ``predict`` event by its uuid; None when absent."""
        wanted = str(prediction_id or "")
        if not wanted:
            return None
        for event in self.query("predict", limit=5000):
            payload = event.get("payload") or {}
            if str(payload.get("prediction_id") or "") == wanted:
                return event
        return None

    def prediction_resolution(self, prediction_id: str) -> dict | None:
        """Newest ``predict_resolved`` event for a prediction, if any."""
        wanted = str(prediction_id or "")
        if not wanted:
            return None
        for event in self.query("predict_resolved", limit=5000):
            payload = event.get("payload") or {}
            if str(payload.get("prediction_id") or "") == wanted:
                return event
        return None

    def equity_curve(self, limit: int = 200) -> list[dict]:
        """Paper equity points from ``cycle_end`` events, oldest first.

        Args:
            limit: Max points (most recent cycles).

        Returns:
            ``[{"ts": ..., "equity_usd": float}, ...]`` ascending by time.
        """
        points: list[dict] = []
        for event in reversed(self.query("cycle_end", limit=limit)):
            payload = event.get("payload") or {}
            value = payload.get("equity_usd")
            if isinstance(value, (int, float)):
                points.append({"ts": event.get("ts"), "equity_usd": float(value)})
        return points

    def marketplace_counts(self) -> list[dict]:
        """Scans / hunches / validations grouped by marketplace name.

        Legacy loop signals have no ``marketplace`` field and are counted
        under the default "polymarket" so history stays visible.

        Returns:
            Per-marketplace count dicts, busiest first.
        """
        stats: dict[str, dict] = {}
        for event in self.query(None, limit=100000):
            event_type = event.get("event_type")
            if event_type not in ("scan", "signal", "validation"):
                continue
            payload = event.get("payload") or {}
            name = str(payload.get("marketplace") or "polymarket") or "polymarket"
            row = stats.setdefault(
                name,
                {
                    "name": name,
                    "scans": 0,
                    "hunches": 0,
                    "validated": 0,
                    "last_scan_at": None,
                },
            )
            if event_type == "scan":
                row["scans"] += 1
                ts = str(event.get("ts") or "")
                if ts and (row["last_scan_at"] is None or ts > row["last_scan_at"]):
                    row["last_scan_at"] = ts
            elif event_type == "signal":
                row["hunches"] += 1
            else:
                row["validated"] += 1
        out: list[dict] = []
        for row in stats.values():
            row["pending_validations"] = max(0, row["hunches"] - row["validated"])
            out.append(row)
        return sorted(out, key=lambda r: (-r["scans"], r["name"]))

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
