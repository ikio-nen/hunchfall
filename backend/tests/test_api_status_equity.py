"""GET /status additive ``equity_curve`` — populated + honest empty."""

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.memory.audit import AuditLog
from app.paths import resolve_db_path
from tests.fakes import make_settings


def _client(tmp_path):
    settings = make_settings(tmp_path)
    return TestClient(create_app(settings)), settings


def test_status_equity_curve_is_ascending_from_cycle_end(tmp_path):
    client, settings = _client(tmp_path)
    audit = AuditLog(resolve_db_path(settings.DATABASE_PATH))
    audit.record("cycle_end", {"equity_usd": 10050.0, "status": "ok"})
    audit.record("cycle_end", {"equity_usd": 10120.5, "status": "ok"})

    body = client.get("/status").json()
    curve = body["equity_curve"]
    assert [point["equity_usd"] for point in curve] == [10050.0, 10120.5]
    assert all(point["ts"] for point in curve)
    assert body["equity_curve_source"] == "cycle_end audit events"
    # existing fields keep working (no loop state yet -> honest None)
    assert body["mode"] == "PAPER"
    assert "bankroll_usd" in body
    assert body["kill_switch"]["engaged"] is False


def test_status_equity_curve_empty_without_cycles(tmp_path):
    client, _settings = _client(tmp_path)
    body = client.get("/status").json()
    assert body["equity_curve"] == []
