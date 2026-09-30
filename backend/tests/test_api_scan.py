"""POST /extension/scan — happy path + two negative paths (offline)."""

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.memory.audit import AuditLog
from app.paths import resolve_db_path
from tests.fakes import (
    FakeClob,
    FakeGamma,
    make_settings,
    patch_scan,
    scan_body,
)


def _client(tmp_path, monkeypatch, *, gamma=None, clob=None):
    settings = make_settings(tmp_path)
    patch_scan(monkeypatch, gamma=gamma, clob=clob)
    return TestClient(create_app(settings)), settings


def test_scan_happy_path_revalidates_and_records(tmp_path, monkeypatch):
    client, settings = _client(tmp_path, monkeypatch)
    resp = client.post("/extension/scan", json=scan_body())
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["mode"] == "PAPER"
    assert body["market"]["condition_id"] == "0xcondition"
    # scraped page price 0.99 is ignored; the live CLOB mid (0.50) wins
    assert body["hunch"]["price"] == 0.50
    assert body["page_state_ignored"] == ["yesPrice", "noPrice"]
    assert body["hunch"]["trade_placed"] is False
    assert body["hunch"]["approved"] is True
    assert body["hunch"]["label"] == "paper prediction · no trade placed"
    assert body["tape"]["count"] == 3
    assert body["tape"]["buy_volume"] == 900.0
    assert body["tape"]["sell_volume"] == 400.0

    # persisted signal, and NO fill — prediction only (RFC decision D2)
    audit = AuditLog(resolve_db_path(settings.DATABASE_PATH))
    signals = audit.query("signal")
    assert len(signals) == 1
    assert signals[0]["payload"]["trade_placed"] is False
    assert signals[0]["payload"]["source"] == "extension"
    assert audit.query("fill") == []
    assert audit.query("scan")


def test_scan_unknown_slug_is_404_and_persists_nothing(tmp_path, monkeypatch):
    client, settings = _client(
        tmp_path, monkeypatch, gamma=lambda s: FakeGamma(s, not_found=True)
    )
    resp = client.post("/extension/scan", json=scan_body())
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "market_not_found"
    audit = AuditLog(resolve_db_path(settings.DATABASE_PATH))
    assert audit.query("signal") == []
    assert audit.query("veto") == []


def test_scan_clob_failure_is_502_and_persists_nothing(tmp_path, monkeypatch):
    client, settings = _client(
        tmp_path,
        monkeypatch,
        clob=lambda s: FakeClob(s, book={"bids": [], "asks": []}),
    )
    resp = client.post("/extension/scan", json=scan_body())
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["code"] == "upstream_error"
    assert detail["source"] == "clob"
    audit = AuditLog(resolve_db_path(settings.DATABASE_PATH))
    assert audit.query("signal") == []
    assert audit.query("stage_error")


def test_scan_vetoed_prediction_is_still_recorded(tmp_path, monkeypatch):
    # 20c spread -> wide_spread veto; the prediction is still persisted
    client, settings = _client(
        tmp_path,
        monkeypatch,
        clob=lambda s: FakeClob(
            s, book={"bids": [[0.40, 5000.0]], "asks": [[0.60, 5000.0]]}
        ),
    )
    resp = client.post("/extension/scan", json=scan_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["hunch"]["approved"] is False
    assert any(v["reason"] == "wide_spread" for v in body["hunch"]["vetoes"])
    audit = AuditLog(resolve_db_path(settings.DATABASE_PATH))
    assert len(audit.query("signal")) == 1
    assert audit.query("veto")
