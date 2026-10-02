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


def test_predict_carries_price_history_features(tmp_path, monkeypatch):
    """``/prices-history`` is now a real model input, not an unused client method."""
    from datetime import datetime, timezone

    now = int(datetime.now(timezone.utc).timestamp())
    history = [
        {"t": now - 86400, "p": 0.40},
        {"t": now - 3600, "p": 0.45},
        {"t": now, "p": 0.50},
    ]
    client, _ = _client(
        tmp_path, monkeypatch, clob=lambda s: FakeClob(s, history=history)
    )
    resp = client.post("/predict", json=predict_body())
    assert resp.status_code == 200, resp.text
    feats = resp.json()["snapshot"]["features"]
    assert feats["price_history"]["change_24h"] == 0.10
    assert feats["price_history"]["change_1h"] == 0.05
    # the tape records which USD definition its numbers use
    assert feats["tape"]["usd_basis"] == "size_x_price"


def test_predict_still_works_when_price_history_is_unavailable(tmp_path, monkeypatch):
    """Price history is a feature: its loss is named, never fatal."""

    class BrokenHistoryClob(FakeClob):
        def get_prices_history(self, token_id, **kwargs):
            raise RuntimeError("history down")

    client, _ = _client(tmp_path, monkeypatch, clob=BrokenHistoryClob)
    resp = client.post("/predict", json=predict_body())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "price_history" in body["snapshot"]["missing"]
    assert "price_history" not in body["snapshot"]["features"]


def test_price_history_never_forces_abstention(tmp_path, monkeypatch):
    """A huge price move is a feature, not a veto — abstention stays edge-based.

    The tape/price-history numbers ride in the snapshot; only the model's edge
    vs the market can abstain.
    """
    from datetime import datetime, timezone

    now = int(datetime.now(timezone.utc).timestamp())
    crash = [
        {"t": now - 86400, "p": 0.90},
        {"t": now - 3600, "p": 0.60},
        {"t": now, "p": 0.05},
    ]
    # Jev returns p_true an edge clear of the market (0.35 vs 0.50 -> -0.15),
    # so the prediction goes out despite the 0.85 collapse in price history.
    client, _ = _client(
        tmp_path,
        monkeypatch,
        clob=lambda s: FakeClob(s, history=crash),
        jev=lambda s: FakeJev(s, p_true=0.35),
    )
    resp = client.post("/predict", json=predict_body())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["snapshot"]["features"]["price_history"]["change_24h"] == -0.85
    assert body["prediction"]["abstained"] is False  # edge-based, not price-based
    assert body["prediction"]["direction"] == "NO"


def test_tape_prefers_cash_and_falls_back_to_the_plain_shape(tmp_path, monkeypatch):
    """If filter_type=CASH is rejected, the tape retries without it."""
    from app.polymarket.data_api import DataApiError

    class CashRejectingTape(FakeTradesApi):
        def __init__(self, settings):
            super().__init__(settings)
            self.seen: list[str | None] = []

        def get_trades_v2(self, condition, limit=100, cursor=None, **kwargs):
            self.seen.append(kwargs.get("filter_type"))
            if kwargs.get("filter_type"):
                raise DataApiError("cash rejected")
            return super().get_trades_v2(condition, limit=limit, cursor=cursor)

    made: list[CashRejectingTape] = []

    def data_api_factory(settings):
        fake = CashRejectingTape(settings)
        made.append(fake)
        return fake

    client, _ = _client(tmp_path, monkeypatch, data_api=data_api_factory)
    resp = client.post("/predict", json=predict_body())
    assert resp.status_code == 200, resp.text
    feats = resp.json()["snapshot"]["features"]
    assert feats["tape"]["usd_basis"] == "size_x_price"
    assert feats["tape"]["buy_usd"] == 906.0  # the fallback rows were used
    assert made[0].seen == ["CASH", None]  # preferred CASH, then plain


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


def test_one_sided_book_abstains_instead_of_failing(tmp_path, monkeypatch):
    """A bids-only book (normal near resolution) yields an honest abstention.

    Live-checked 2026-10-02 on the near-resolution BTC market: YES bids only,
    asks []. The predictor used to 502 the whole request; it now keeps the
    available side, resolves the mid via /midpoint, and abstains on data
    quality — a logged prediction, never a guess.
    """
    one_sided = {
        "bids": [[0.99, 1000.0]],
        "asks": [],
        "last_trade_price": "0.995",
    }
    client, settings = _client(
        tmp_path,
        monkeypatch,
        clob=lambda s: FakeClob(s, book=one_sided, midpoint=0.998),
    )
    resp = client.post("/predict", json=predict_body())
    assert resp.status_code == 200, resp.text
    body = resp.json()

    feats = body["snapshot"]["features"]
    assert feats["one_sided"] == "ask"
    assert feats["best_bid"] == 0.99
    assert feats["bid_depth_usd"] == 990.0
    assert "imbalance" not in feats
    assert feats["market_mid"] == 0.998
    assert feats["mid_source"] == "midpoint"
    assert body["snapshot"]["missing"] == ["book_ask"]

    prediction = body["prediction"]
    assert prediction["abstained"] is True
    assert prediction["direction"] == "ABSTAIN"
    assert prediction["p_yes"] == 0.8  # ensemble still ran
    assert prediction["edge_vs_market"] == -0.198  # computed, then overridden
    assert any(
        "abstained: one-sided book (no ask side) — data-quality abstention "
        "(mid 0.998 via /midpoint)" == reason
        for reason in prediction["reasons"]
    )
    # an abstention is still logged (honesty), never a guess
    assert len(_audit(settings).predictions()) == 1


def test_one_sided_book_falls_back_to_last_trade_price(tmp_path, monkeypatch):
    """When /midpoint fails too, the last traded price is the anchor."""
    one_sided = {"bids": [[0.90, 500.0]], "asks": [], "last_trade_price": "0.91"}
    client, _ = _client(
        tmp_path,
        monkeypatch,
        clob=lambda s: FakeClob(s, book=one_sided, midpoint_fail=True),
    )
    body = client.post("/predict", json=predict_body()).json()
    feats = body["snapshot"]["features"]
    assert feats["market_mid"] == 0.91
    assert feats["mid_source"] == "last_trade"
    assert body["prediction"]["abstained"] is True
    assert any("via /last_trade" in reason for reason in body["prediction"]["reasons"])


def test_one_sided_book_with_no_defensible_mid_is_502(tmp_path, monkeypatch):
    """No /midpoint and no last trade -> fail closed, never guess."""
    one_sided = {"bids": [[0.90, 500.0]], "asks": []}
    client, settings = _client(
        tmp_path,
        monkeypatch,
        clob=lambda s: FakeClob(s, book=one_sided, midpoint_fail=True),
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


def test_demo_live_one_sided_book_abstains(tmp_path, monkeypatch):
    """The demo path uses the same data-quality abstention and never persists."""
    client, settings = _client(
        tmp_path,
        monkeypatch,
        PREDICT_DEMO_LIVE=True,
        clob=lambda s: FakeClob(
            s, book={"bids": [[0.99, 100.0]], "asks": []}, midpoint=0.998
        ),
    )
    body = client.get("/predict/demo").json()
    assert body["snapshot_source"] == "live"
    assert body["prediction"]["abstained"] is True
    assert body["prediction"]["direction"] == "ABSTAIN"
    assert _audit(settings).predictions() == []


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


def test_abstain_edge_of_zero_never_abstains(tmp_path, monkeypatch):
    """A deliberate PREDICT_ABSTAIN_EDGE=0.0 must not be coerced back to 0.10."""
    client, _ = _client(
        tmp_path,
        monkeypatch,
        PREDICT_ABSTAIN_EDGE=0.0,
        jev=lambda settings: FakeJev(settings, p_true=0.53),
    )
    body = client.post("/predict", json=predict_body()).json()
    # p_yes 0.53 vs mid 0.50 -> edge +0.03: a guess at threshold 0.0, an
    # abstention at the 0.10 default (pinned by the companion test below).
    assert body["abstain_threshold"] == 0.0
    assert body["prediction"]["abstained"] is False
    assert body["prediction"]["direction"] == "YES"


def test_the_same_edge_abstains_at_the_default_threshold(tmp_path, monkeypatch):
    """Companion to the test above: that edge really is below the default."""
    client, _ = _client(
        tmp_path, monkeypatch, jev=lambda settings: FakeJev(settings, p_true=0.53)
    )
    body = client.post("/predict", json=predict_body()).json()
    assert body["abstain_threshold"] == 0.10
    assert body["prediction"]["abstained"] is True


def test_client_construction_failure_is_a_502_envelope(tmp_path, monkeypatch):
    """A client that cannot even be built is an upstream error, not a 500."""

    class Exploding:
        def __init__(self, settings):
            raise RuntimeError("no gamma today")

    client, settings = _client(tmp_path, monkeypatch, gamma=Exploding)
    resp = client.post("/predict", json=predict_body())
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["code"] == "upstream_error"
    assert detail["source"] == "gamma"
    assert _audit(settings).predictions() == []  # fail closed: nothing logged


def test_predict_asks_clob_for_clean_token_ids(tmp_path, monkeypatch):
    """Regression: clobTokenIds is a JSON-array string; a comma split sent
    CLOB an id wrapped in ``["`` … ``"]`` and every book came back empty."""
    made: list[FakeClob] = []

    def clob_factory(settings):
        clob = FakeClob(settings)
        made.append(clob)
        return clob

    client, _ = _client(tmp_path, monkeypatch, clob=clob_factory)
    assert client.post("/predict", json=predict_body()).status_code == 200
    assert made[0].token_ids == ["111"]  # the YES token, unquoted


def test_predict_by_condition_id_resolves_and_records(tmp_path, monkeypatch):
    """The condition_id path goes through Gamma's condition_ids filter."""
    client, settings = _client(tmp_path, monkeypatch)
    resp = client.post("/predict", json={"condition_id": "0x" + "a" * 64})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["market"]["condition_id"] == "0xcondition"
    assert body["label"] == "paper prediction · no trade placed"
    assert body["trade_placed"] is False
    assert len(_audit(settings).predictions()) == 1


def test_predict_unknown_condition_id_is_404(tmp_path, monkeypatch):
    client, settings = _client(
        tmp_path, monkeypatch, gamma=lambda s: FakeGamma(s, not_found=True)
    )
    resp = client.post("/predict", json={"condition_id": "0x" + "b" * 64})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "market_not_found"
    assert _audit(settings).predictions() == []  # fail closed: nothing logged


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


def test_accuracy_limit_is_clamped(tmp_path, monkeypatch):
    """``limit`` is a scan cap, not an unbounded page request."""
    client, settings = _client(tmp_path, monkeypatch)
    audit = _audit(settings)
    audit.record("predict", _predict_payload("p1", 0.70, 0.60))
    audit.record("predict", _predict_payload("p2", 0.30, 0.40))

    assert client.get("/predict/accuracy?limit=1").json()["n_logged"] == 1
    # 0 and negatives clamp up to 1; huge values clamp down to 5000
    assert client.get("/predict/accuracy?limit=0").json()["n_logged"] == 1
    assert client.get("/predict/accuracy?limit=-5").json()["n_logged"] == 1
    assert client.get("/predict/accuracy?limit=999999").json()["n_logged"] == 2


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
