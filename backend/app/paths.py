"""Shared filesystem locations under backend/.

Both the trading loop and the API read/write the same state and kill-switch
files; this module is the single place that defines where they live.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def backend_root() -> Path:
    """Absolute path of the backend/ directory."""
    return Path(__file__).resolve().parents[1]


def data_dir() -> Path:
    """backend/data/, created on demand."""
    d = backend_root() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_path() -> Path:
    """backend/data/state.json — last loop-written portfolio snapshot."""
    return data_dir() / "state.json"


def killswitch_path() -> Path:
    """backend/data/killswitch.json — kill-switch engagement record."""
    return data_dir() / "killswitch.json"


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def read_killswitch() -> dict:
    """Read the kill-switch record; defaults to disengaged when missing.

    Returns:
        ``{"engaged": bool, "reason": str | None, "engaged_at": str | None}``.
    """
    p = killswitch_path()
    if not p.exists():
        return {"engaged": False, "reason": None, "engaged_at": None}
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {"engaged": False, "reason": "unreadable", "engaged_at": None}
    return {
        "engaged": bool(data.get("engaged", False)),
        "reason": data.get("reason"),
        "engaged_at": data.get("engaged_at"),
    }


def write_killswitch(engaged: bool, reason: str | None) -> dict:
    """Atomically write the kill-switch record.

    Args:
        engaged: Whether the kill switch is engaged.
        reason: Human-readable reason ("manual", "auto: ...", or None).

    Returns:
        The written record.
    """
    payload = {
        "engaged": engaged,
        "reason": reason,
        "engaged_at": utcnow_iso() if engaged else None,
    }
    p = killswitch_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(p)
    return payload


def resolve_db_path(database_path: str) -> str:
    """Resolve DATABASE_PATH: relative paths are anchored at backend/.

    Args:
        database_path: Raw value from Settings.

    Returns:
        Absolute path string.
    """
    p = Path(str(database_path))
    if p.is_absolute():
        return str(p)
    return str(backend_root() / p)
