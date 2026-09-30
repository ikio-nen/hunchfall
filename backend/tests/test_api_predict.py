"""RFC-003 prediction routes — one happy path + two negatives each (offline).

Every upstream (Gamma/CLOB/Data API/Jev/Jetstream/Reddit/RSS) is faked; the
suite asserts zero network and that a prediction never places a fill.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.memory.audit import AuditLog
from app.paths import resolve_db_path
from tests.fakes import (
    FakeClob,
    FakeGamma,
    FakeJev,
    FakeTradesApi,
    make_settings,
    patch_predict,
    patch_social,
    predict_body,
)


def _client(tmp_path, monkeypatch, *, gamma=None, clob=None, data_api=None, jev=None, **settings):
    merged = make_settings(tmp_path, **settings)
    patch_predict(monkeypatch, gamma=gamma, clob=clob, data_api=data_api, jev=jev)
    patch_social(monkeypatch)  # no real social source is ever contacted
    return TestClient(create_app(merged)), merged


def _audit(settings) -> AuditLog:
    return AuditLog(resolve_db_path(settings.DATABASE_PATH))


def _predict_payload(
    pid: str, p_yes: float, market: float, *, abstained: bool = False, mock: bool = False
) -> dict:
    """A seeded ``predict`` event payload with hand-chosen numbers."""
    return {
        "prediction_id": pid,
        "condition_id": "0xcond",
        "slug": "seeded-market",
        "question": "Seeded question?",
        "p_yes": p_yes,
        "market_price": market,
        "edge_vs_market": round(p_yes - market, 4),
        "direction": "ABSTAIN" if abstained else ("YES" if p_yes >= market else "NO"),
        "confidence": 0.5,
        "abstained": abstained,
        "abstain_threshold": 0.10,
        "model": {"mode": "mock" if mock else "live", "mock": mock, "version": "jev-mock"},
        "snapshot_sha256": "deadbeef",
        "reasons": ["r"],
    }


# --------------------------------------------------------------- POST /predict
def test_predict_happy_path_records_a_prediction(tmp_path, monkeypatch):
    client, settings = _client(tmp_path, monkeypatch)
    resp = client.post("/predict", json=predict_body())
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["mode"] == "PAPER"
    assert body["label"] == "paper prediction · no trade placed"
    assert body["trade_placed"] is False
    assert body["market"]["condition_id"] == "0xcondition"
    assert body["market_price"] == 0.5
    assert body["abstain_threshold"] == 0.10

    prediction = body["prediction"]
    # the fake Jev returns p_true = 0.8 vs mid 0.50 -> edge +0.30 -> YES
    assert prediction["p_yes"] == 0.8
    assert prediction["direction"] == "YES"
    assert prediction["edge_vs_market"] == 0.3
    assert prediction["abstained"] is False
    assert prediction["reasons"]  # typed reasons, never empty
    assert body["snapshot"]["sha256"]
    assert body["snapshot"]["missing"] == []  # tape + social both present

    # the tape came from /v2/trades with USD = size x price
    tape = body["snapshot"]["features"]["tape"]
    assert tape["buy_usd"] == 906.0
    assert tape["sell_usd"] == 392.0

    audit = _audit(settings)
    predictions = audit.predictions()
    assert len(predictions) == 1
    assert predictions[0]["payload"]["prediction_id"] == prediction["prediction_id"]
    assert audit.query("fill") == []  # prediction only — never a fill


def test_predict_unknown_slug_is_404_and_persists_nothing(tmp_path, monkeypatch):
    client, settings = _client(
        tmp_path, monkeypatch, gamma=lambda s: FakeGamma(s, not_found=True)
    )
    resp = client.post("/predict", json=predict_body())
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "market_not_found"
    assert _audit(settings).predictions() == []


def test_predict_empty_book_is_502_and_persists_nothing(tmp_path, monkeypatch):
    client, settings = _client(
        tmp_path, monkeypatch, clob=lambda s: FakeClob(s, book={"bids": [], "asks": []})
    )
    resp = client.post("/predict", json=predict_body())
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["code"] == "upstream_error"
    assert detail["source"] == "clob"
    audit = _audit(settings)
    assert audit.predictions() == []
    assert audit.query("stage_error")


def test_predict_requires_exactly_one_identifier(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    assert client.post("/predict", json={}).status_code == 422
    both = predict_body(condition_id="0x" + "a" * 64)
    assert client.post("/predict", json=both).status_code == 422
    assert client.post("/predict", json={"nope": 1}).status_code == 422


def test_predict_abstains_below_the_threshold(tmp_path, monkeypatch):
    # p_true 0.55 vs mid 0.50 -> edge +0.05 < 0.10 -> ABSTAIN, still 200
    client, settings = _client(
        tmp_path, monkeypatch, jev=lambda s: FakeJev(s, p_true=0.55, choice="SKIP")
    )
    resp = client.post("/predict", json=predict_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["prediction"]["direction"] == "ABSTAIN"
    assert body["prediction"]["abstained"] is True
    assert any("abstained" in reason for reason in body["prediction"]["reasons"])
    # an abstention is still a logged prediction (honesty), just not a guess
    assert len(_audit(settings).predictions()) == 1


def test_predict_mock_is_labelled(tmp_path, monkeypatch):
    client, _ = _client(
        tmp_path, monkeypatch, jev=lambda s: FakeJev(s, p_true=0.8, mock=True)
    )
    body = client.post("/predict", json=predict_body()).json()
    assert body["model"]["mock"] is True
    assert body["model"]["mode"] == "mock"
    assert body["model"]["note"] == "MOCK — not a real model"


# ------------------------------------------------------------ GET /predict/demo
def test_demo_canned_is_deterministic_and_never_persists(tmp_path, monkeypatch):
    client, settings = _client(
        tmp_path, monkeypatch, PREDICT_DEMO_LIVE=False
    )
    resp = client.get("/predict/demo")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["demo"] is True
    assert body["snapshot_source"] == "canned"
    assert body["prediction"]["abstained"] is False  # pinned non-abstained guess
    assert body["model"]["mock"] is True
    assert body["audit_event_id"] is None
    assert body["trade_placed"] is False
    assert _audit(settings).predictions() == []


def test_demo_live_path_uses_real_upstreams(tmp_path, monkeypatch):
    client, settings = _client(tmp_path, monkeypatch, PREDICT_DEMO_LIVE=True)
    body = client.get("/predict/demo").json()
    assert body["snapshot_source"] == "live"
    assert _audit(settings).predictions() == []  # demo never writes the ledger


def test_demo_falls_back_to_canned_when_upstreams_fail(tmp_path, monkeypatch):
    client, settings = _client(
        tmp_path,
        monkeypatch,
        PREDICT_DEMO_LIVE=True,
        gamma=lambda s: FakeGamma(s, not_found=True),
    )
    resp = client.get("/predict/demo")
    assert resp.status_code == 200  # always 200
    assert resp.json()["snapshot_source"] == "canned"
    assert _audit(settings).predictions() == []


# -------------------------------------------------------- GET /predict/accuracy
def test_accuracy_is_derived_on_read(tmp_path, monkeypatch):
    client, settings = _client(tmp_path, monkeypatch)
    audit = _audit(settings)
    audit.record("predict", _predict_payload("p1", 0.70, 0.60))
    audit.record("predict", _predict_payload("p2", 0.30, 0.40))
    audit.record("predict", _predict_payload("p3", 0.50, 0.50, abstained=True))
    audit.record("predict_resolved", {"prediction_id": "p1", "outcome": "YES"})
    audit.record("predict_resolved", {"prediction_id": "p2", "outcome": "NO"})

    body = client.get("/predict/accuracy").json()
    assert body["mode"] == "PAPER"
    assert body["include_mock"] is False
    assert body["n_logged"] == 3
    assert body["n_resolved"] == 2
    assert body["brier"]["model"] == 0.09
    assert body["brier"]["market"] == 0.16
    assert body["direction_accuracy"] == 1.0


def test_accuracy_empty_ledger_is_zeros(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    body = client.get("/predict/accuracy").json()
    assert body["n_logged"] == 0
    assert body["brier"] is None
    assert body["mock_split"]["live"] == {"n_logged": 0, "n_resolved": 0}


def test_accuracy_excludes_mock_unless_asked(tmp_path, monkeypatch):
    client, settings = _client(tmp_path, monkeypatch)
    audit = _audit(settings)
    audit.record("predict", _predict_payload("m1", 0.9, 0.5, mock=True))
    audit.record("predict_resolved", {"prediction_id": "m1", "outcome": "YES"})

    excluded = client.get("/predict/accuracy").json()
    assert excluded["n_logged"] == 0
    assert excluded["mock_split"]["mock"] == {"n_logged": 1, "n_resolved": 1}

    included = client.get("/predict/accuracy?include_mock=true").json()
    assert included["n_logged"] == 1
    assert included["include_mock"] is True


# -------------------------------------------------- POST /predict/{id}/resolve
def test_resolve_records_the_outcome(tmp_path, monkeypatch):
    client, settings = _client(tmp_path, monkeypatch)
    audit = _audit(settings)
    audit.record("predict", _predict_payload("p1", 0.70, 0.60))

    resp = client.post("/predict/p1/resolve", json={"outcome": "YES"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["prediction_id"] == "p1"
    assert body["outcome"] == "YES"
    assert body["brier"]["model"] == 0.09  # (0.7 - 1)^2
    assert body["brier"]["market"] == 0.16
    assert body["label"] == "paper prediction · no trade placed"
    assert len(_audit(settings).prediction_resolutions()) == 1


def test_resolve_unknown_prediction_is_404(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.post("/predict/nope/resolve", json={"outcome": "YES"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "unknown_prediction"


def test_resolve_duplicate_is_409(tmp_path, monkeypatch):
    client, settings = _client(tmp_path, monkeypatch)
    _audit(settings).record("predict", _predict_payload("p1", 0.70, 0.60))
    first = client.post("/predict/p1/resolve", json={"outcome": "YES"})
    assert first.status_code == 200
    second = client.post("/predict/p1/resolve", json={"outcome": "NO"})
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "already_resolved"
    # the first verdict is unchanged
    assert len(_audit(settings).prediction_resolutions()) == 1
