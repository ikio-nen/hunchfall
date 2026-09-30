"""Read-only monitor API for hunchfall (paper mode).

All data comes from the SQLite audit log and ``data/state.json`` written by
the trading loop. Everything is labeled PAPER. The only mutating endpoints
are the kill switch (POST /kill, POST /resume) — nothing here can place a
real order, move money, or touch a wallet.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.memory.audit import AuditLog
from app.paths import (
    read_killswitch,
    resolve_db_path,
    state_path,
    write_killswitch,
)


def _cors_origins(settings) -> list[str]:
    """Accept CORS_ORIGINS as a comma-separated string or a list."""
    raw = getattr(settings, "CORS_ORIGINS", "")
    if isinstance(raw, (list, tuple)):
        return [str(o).strip() for o in raw if str(o).strip()]
    return [o.strip() for o in str(raw).split(",") if o.strip()]


def _read_state() -> dict:
    """Read the loop-written state snapshot; honest placeholder if absent."""
    p = state_path()
    if not p.exists():
        return {
            "mode": "PAPER",
            "note": "no state yet — the loop has not run",
            "positions": [],
            "signals": [],
            "vetoes": [],
            "fills": [],
            "calibration": {"note": "no calibration yet", "bins": []},
        }
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {
            "mode": "PAPER",
            "note": "state.json unreadable",
            "positions": [],
            "signals": [],
            "vetoes": [],
            "fills": [],
            "calibration": {"note": "no calibration yet", "bins": []},
        }


def create_app(settings) -> FastAPI:
    """Build the FastAPI app.

    Args:
        settings: App Settings (CORS_ORIGINS, DATABASE_PATH, ...).

    Returns:
        Configured FastAPI application.
    """
    app = FastAPI(
        title="hunchfall (paper)",
        version="0.1.0",
        description="Paper-trading monitor API. All data is simulated; "
        "nothing here places real orders.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(settings) or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    audit = AuditLog(resolve_db_path(settings.DATABASE_PATH))
    app.state.settings = settings
    app.state.audit = audit

    @app.get("/")
    def root() -> dict:
        return {"service": "hunchfall", "mode": "PAPER", "docs": "/docs"}

    @app.get("/status")
    def status() -> dict:
        """Bankroll, exposure, equity, kill-switch state — all paper."""
        state = _read_state()
        return {
            "service": "hunchfall",
            "mode": "PAPER",
            "bankroll_usd": state.get("bankroll_usd"),
            "cash_usd": state.get("cash_usd"),
            "exposure_usd": state.get("exposure_usd"),
            "equity_usd": state.get("equity_usd"),
            "drawdown_pct": state.get("drawdown_pct"),
            "kill_switch": read_killswitch(),
            "updated_at": state.get("updated_at"),
            "audit": audit.get_status(),
        }

    @app.get("/positions")
    def positions() -> dict:
        return {"mode": "PAPER", "positions": _read_state().get("positions", [])}

    @app.get("/signals")
    def signals(limit: int = 200) -> dict:
        return {"mode": "PAPER", "signals": audit.query("signal", limit=limit)}

    @app.get("/vetoes")
    def vetoes(limit: int = 200) -> dict:
        return {"mode": "PAPER", "vetoes": audit.query("veto", limit=limit)}

    @app.get("/fills")
    def fills(limit: int = 200) -> dict:
        return {"mode": "PAPER", "fills": audit.query("fill", limit=limit)}

    @app.get("/calibration")
    def calibration() -> dict:
        return {
            "mode": "PAPER",
            "calibration": _read_state().get(
                "calibration", {"note": "no calibration yet", "bins": []}
            ),
        }

    @app.post("/kill")
    def kill(body: dict[str, Any] | None = None) -> dict:
        """Engage the kill switch (manual). The loop halts on its next check."""
        reason = (body or {}).get("reason", "manual")
        record = write_killswitch(True, str(reason))
        audit.record("killswitch", {"manual": True, "reason": record["reason"]})
        return {"mode": "PAPER", "kill_switch": record}

    @app.post("/resume")
    def resume() -> dict:
        """Clear the kill switch so the loop may run again."""
        record = write_killswitch(False, None)
        audit.record("killswitch", {"manual": True, "reason": "resumed"})
        return {"mode": "PAPER", "kill_switch": record}

    @app.get("/config")
    def config() -> dict:
        """Non-secret operational config (keys are stripped by safe_subset)."""
        return {"mode": "PAPER", "config": settings.safe_subset()}

    return app
