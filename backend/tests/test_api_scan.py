"""POST /extension/scan — happy path + two negative paths (offline)."""

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.memory.audit import AuditLog
from app.paths import resolve_db_path
from tests.fakes import (
    FakeClob,
    FakeDataApi,
    FakeGamma,
    make_settings,
    patch_scan,
    scan_body,
)


def _client(tmp_path, monkeypatch, *, gamma=None, clob=None, data_api=None):
    settings = make_settings(tmp_path)
    patch_scan(monkeypatch, gamma=gamma, clob=clob, data_api=data_api)
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
    # the fakes carry size (shares) AND usdc_size (USD); the tape sums USD only
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


def test_scan_tape_uses_usdc_notional_not_shares(tmp_path, monkeypatch):
    """v2 tape volumes must come from usdc_size (USD), never size (shares)."""
    items = [
        {
            "side": "BUY",
            "size": 2000.0,  # shares — must not be summed as USD
            "usdc_size": 250.0,  # USD notional
            "price": 0.50,
            "timestamp": 1759224000,
            "title": "Will it rain tomorrow?",
            "transaction_hash": "0x" + "ab" * 32,
        },
        {
            "side": "SELL",
            "size": 400.0,
            "usdc_size": 100.0,
            "price": 0.51,
            "timestamp": 1759224600,
            "title": "Will it rain tomorrow?",
            "transaction_hash": "0x" + "cd" * 32,
        },
    ]
    client, _settings = _client(
        tmp_path, monkeypatch, data_api=lambda s: FakeDataApi(s, items=items)
    )
    resp = client.post("/extension/scan", json=scan_body())
    assert resp.status_code == 200, resp.text
    tape = resp.json()["tape"]
    assert tape["buy_volume"] == 250.0
    assert tape["sell_volume"] == 100.0
    assert tape["flow_imbalance"] == 0.4286


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
