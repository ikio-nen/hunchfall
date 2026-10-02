"""OI + holder-concentration features: predictor, model text, loop parity.

The payload shapes pinned here are the live-checked 2026-10-03 Data API v2
responses (``/v2/oi?condition=`` and ``/v2/holders?condition=``); every
upstream is a local fake, so the suite is zero-network.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.loop import _snapshot, _snapshots
from app.memory.audit import AuditLog
from app.paths import resolve_db_path
from app.polymarket.data_api import top10_holder_share
from app.predict import build_snapshot_text
from tests.fakes import (
    FakeClob,
    FakeTradesApi,
    holder_groups,
    make_settings,
    patch_predict,
    patch_social,
    predict_body,
)


class _RecordingAudit:
    """Minimal audit sink: records events, never touches SQLite."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event_type: str, payload: dict) -> int:
        self.events.append((event_type, payload))
        return len(self.events)


def _client(tmp_path, monkeypatch, *, data_api=None):
    settings = make_settings(tmp_path)
    patch_predict(monkeypatch, data_api=data_api)
    patch_social(monkeypatch)  # no real social source is ever contacted
    return TestClient(create_app(settings)), settings


def _audit(settings) -> AuditLog:
    return AuditLog(resolve_db_path(settings.DATABASE_PATH))


# ------------------------------------------------------ pure share definition
def test_top10_share_is_the_ten_largest_fraction_of_all_listed_amounts():
    # 100 + ten 10s: total 200, top ten = 100 + 9*10 = 190 -> 0.95 exactly
    assert top10_holder_share(holder_groups()) == 0.95


def test_top10_share_fewer_than_ten_holders_is_one():
    groups = [{"token_id": "111", "holders": [{"amount": 5.0}, {"amount": 3.0}]}]
    assert top10_holder_share(groups) == 1.0


def test_top10_share_no_usable_amounts_is_none():
    assert top10_holder_share([]) is None
    assert top10_holder_share([{"token_id": "111", "holders": []}]) is None
    assert (
        top10_holder_share(
            [{"token_id": "111", "holders": [{"amount": "junk"}, {"amount": None}]}]
        )
        is None
    )
    assert top10_holder_share("not-a-list") is None


def test_top10_share_skips_malformed_rows_instead_of_failing():
    groups = [
        "junk",
        {"token_id": "111", "holders": ["junk", {"amount": 30.0}, {"amount": -5.0}]},
        {"token_id": "222", "holders": [{"amount": 70.0}, {"nope": 1}]},
    ]
    # usable amounts 30 + 70 -> both are in the top ten -> 1.0
    assert top10_holder_share(groups) == 1.0


# -------------------------------------------------------------- /predict wiring
def test_predict_carries_oi_and_holder_concentration(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    body = client.post("/predict", json=predict_body()).json()
    feats = body["snapshot"]["features"]
    assert feats["oi"] == 88_500.0
    assert feats["holders"] == 0.95
    assert body["snapshot"]["missing"] == []  # tape + social + oi + holders


def test_predict_names_missing_oi_and_holders_and_still_answers(
    tmp_path, monkeypatch
):
    client, _ = _client(
        tmp_path,
        monkeypatch,
        data_api=lambda s: FakeTradesApi(s, oi=None, holders=[]),
    )
    resp = client.post("/predict", json=predict_body())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["snapshot"]["missing"] == ["oi", "holders"]
    assert "oi" not in body["snapshot"]["features"]
    assert "holders" not in body["snapshot"]["features"]
    assert any(
        "missing feature source: oi" in reason
        for reason in body["prediction"]["reasons"]
    )


def test_predict_a_dead_oi_endpoint_is_a_named_degradation_not_a_500(
    tmp_path, monkeypatch
):
    client, settings = _client(
        tmp_path,
        monkeypatch,
        data_api=lambda s: FakeTradesApi(s, fail_oi=True, fail_holders=True),
    )
    resp = client.post("/predict", json=predict_body())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["snapshot"]["missing"] == ["oi", "holders"]
    assert body["snapshot"]["features"]["tape"]["count"] == 3  # the rest is intact
    stages = sorted(
        row["payload"].get("stage")
        for row in _audit(settings).query("stage_error")
    )
    assert stages == ["data-api.holders", "data-api.oi"]


# ------------------------------------------------------------------ model text
def test_snapshot_text_shows_oi_and_holder_concentration():
    features = {"market_mid": 0.5, "oi": 88_500.0, "holders": 0.95}
    text = build_snapshot_text("Will it rain tomorrow?", 0.5, features, [])
    assert "Open interest: $88,500" in text
    assert "top-10 holders hold 95.0% of the listed token amounts" in text


def test_snapshot_text_omits_absent_oi_and_holders():
    text = build_snapshot_text(
        "Will it rain tomorrow?", 0.5, {"market_mid": 0.5}, ["oi", "holders"]
    )
    assert "Open interest" not in text
    assert "Holder concentration" not in text
    assert "Missing feature sources: oi, holders" in text


# ----------------------------------------------------------------- loop parity
def test_loop_snapshot_carries_the_same_two_features(tmp_path):
    settings = make_settings(tmp_path)
    data_api = FakeTradesApi(settings)
    snap = _snapshot(FakeClob(settings), data_api, "0xcond", "111", _RecordingAudit())
    assert snap["oi"] == 88_500.0
    assert snap["holders"] == 0.95
    assert snap["missing"] == []
    assert data_api.oi_calls == [{"condition": "0xcond"}]
    assert data_api.holders_calls == [{"condition": "0xcond"}]


def test_loop_snapshot_degrades_honestly_when_oi_and_holders_fail(tmp_path):
    settings = make_settings(tmp_path)
    data_api = FakeTradesApi(settings, fail_oi=True, fail_holders=True)
    audit = _RecordingAudit()
    snap = _snapshot(FakeClob(settings), data_api, "0xcond", "111", audit)
    assert snap is not None  # the market snapshot still exists
    assert snap["oi"] is None
    assert snap["holders"] is None
    assert snap["missing"] == ["oi", "holders"]
    assert [payload.get("stage") for _, payload in audit.events] == [
        "data-api.oi",
        "data-api.holders",
    ]


def test_loop_snapshot_missing_payload_is_named_never_zero(tmp_path):
    settings = make_settings(tmp_path)
    data_api = FakeTradesApi(settings, oi=None, holders=[])
    snap = _snapshot(FakeClob(settings), data_api, "0xcond", "111", _RecordingAudit())
    assert snap["oi"] is None
    assert snap["holders"] is None
    assert snap["missing"] == ["oi", "holders"]


def test_batched_snapshots_get_per_market_features(tmp_path):
    settings = make_settings(tmp_path)
    data_api = FakeTradesApi(settings, items=[])
    snaps = _snapshots(
        FakeClob(settings),
        data_api,
        [
            {"market_id": "0xa", "token_id": "111"},
            {"market_id": "0xb", "token_id": "222"},
        ],
        _RecordingAudit(),
    )
    assert snaps["0xa"]["oi"] == 88_500.0
    assert snaps["0xb"]["holders"] == 0.95
    assert [call["condition"] for call in data_api.oi_calls] == ["0xa", "0xb"]
