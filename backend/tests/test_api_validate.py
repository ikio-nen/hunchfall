"""POST /signals/{id}/validate — happy + 404 + 409 (offline)."""

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.memory.audit import AuditLog
from app.paths import resolve_db_path
from tests.fakes import FakeClob, make_settings, patch_scan, scan_body


def _app(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    patch_scan(monkeypatch)
    # the validate route re-checks CLOB — keep it offline
    monkeypatch.setattr("app.api.server.ClobClient", FakeClob)
    audit = AuditLog(resolve_db_path(settings.DATABASE_PATH))
    return TestClient(create_app(settings)), settings, audit


def _scan(client) -> dict:
    resp = client.post("/extension/scan", json=scan_body())
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_validate_happy_path_updates_calibration(tmp_path, monkeypatch):
    client, _settings, _audit = _app(tmp_path, monkeypatch)
    hunch = _scan(client)["hunch"]

    resp = client.post(
        f"/signals/{hunch['signal_id']}/validate", json={"outcome": "HIT"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "HIT"
    assert body["signal_id"] == hunch["signal_id"]
    assert body["label"] == "paper prediction · no trade placed"
    assert body["calibration"]["n_validated"] == 1
    assert body["calibration"]["accuracy"] == 1.0
    # p_true 0.8, side YES, HIT -> yes_outcome 1 -> brier (0.8 - 1)^2 = 0.04
    assert body["calibration"]["brier"] == pytest.approx(0.04)
    # evidence came from the (fake) CLOB re-check
    assert body["evidence"]["price_at_validate"] == 0.50

    cal = client.get("/calibration").json()["calibration"]
    assert cal["n_validated"] == 1
    assert cal["accuracy"] == 1.0
    assert any(bin_["n"] == 1 for bin_ in cal["bins"])


def test_validate_unknown_signal_is_404(tmp_path, monkeypatch):
    client, _settings, audit = _app(tmp_path, monkeypatch)
    resp = client.post("/signals/does-not-exist/validate", json={"outcome": "HIT"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "unknown_signal"
    assert audit.query("validation") == []


def test_validate_duplicate_is_409_and_not_double_counted(tmp_path, monkeypatch):
    client, _settings, audit = _app(tmp_path, monkeypatch)
    hunch = _scan(client)["hunch"]
    first = client.post(
        f"/signals/{hunch['signal_id']}/validate", json={"outcome": "MISS"}
    )
    assert first.status_code == 200
    second = client.post(
        f"/signals/{hunch['signal_id']}/validate", json={"outcome": "HIT"}
    )
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "already_validated"
    assert "MISS" in second.json()["detail"]["message"]
    assert len(audit.query("validation")) == 1


def test_validate_loop_signal_by_row_id(tmp_path, monkeypatch):
    """Legacy loop signals (no signal_id) validate via their audit row id."""
    client, _settings, audit = _app(tmp_path, monkeypatch)
    row_id = audit.record(
        "signal",
        {
            "question": "Legacy loop signal?",
            "p_true": 0.70,
            "market_price": 0.50,
            "token_id": "111",
            "marketplace": "polymarket",
        },
    )
    resp = client.post(f"/signals/{row_id}/validate", json={"outcome": "HIT"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["signal_id"] == f"row:{row_id}"
    assert resp.json()["signal"]["side"] == "YES"


def test_validate_rejects_bad_outcome(tmp_path, monkeypatch):
    client, _settings, _audit = _app(tmp_path, monkeypatch)
    hunch = _scan(client)["hunch"]
    resp = client.post(
        f"/signals/{hunch['signal_id']}/validate", json={"outcome": "MAYBE"}
    )
    assert resp.status_code == 422
