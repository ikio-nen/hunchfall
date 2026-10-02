"""Loop snapshots take the tape in one batched ``/v2/trades`` call.

Before this, a cycle with N candidate markets paid N tape round-trips (one per
``_snapshot``). The Data API accepts up to 20 comma-separated condition ids in
a single request (live-checked 2026-10-02), so ``_snapshots`` fetches once and
splits rows client-side. Books stay per-token: the CLOB batch endpoints are
not usable on this deployment.

Zero network: the CLOB book and the tape are offline fakes.
"""

from __future__ import annotations

from app.loop import _mark_positions, _snapshots
from tests.fakes import FakeClob, FakeTradesApi, make_settings


class _RecordingAudit:
    """Minimal audit sink: records events, never touches SQLite."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event_type: str, payload: dict) -> int:
        self.events.append((event_type, payload))
        return len(self.events)


def _row(condition: str, ts: int) -> dict:
    return {
        "condition_id": condition,
        "price": 0.5,
        "size": 10.0,
        "side": "BUY",
        "timestamp": ts,
        "token_id": "111",
    }


def test_snapshots_fetch_the_tape_once_for_many_markets(tmp_path):
    settings = make_settings(tmp_path)
    data_api = FakeTradesApi(
        settings,
        items=[_row("0xa", 1000), _row("0xb", 2000), _row("0xa", 3000)],
    )
    audit = _RecordingAudit()
    snaps = _snapshots(
        FakeClob(settings),
        data_api,
        [
            {"market_id": "0xa", "token_id": "111"},
            {"market_id": "0xb", "token_id": "222"},
        ],
        audit,
    )
    # one HTTP call for both markets, comma-joined
    assert len(data_api.calls) == 1
    assert data_api.calls[0]["condition"] == "0xa,0xb"
    assert set(snaps) == {"0xa", "0xb"}
    assert snaps["0xa"]["mid_price"] == 0.5


def test_each_market_gets_its_own_last_trade_ts(tmp_path):
    """The batched rows must not leak one market's newest trade into another."""
    settings = make_settings(tmp_path)
    data_api = FakeTradesApi(
        settings,
        items=[_row("0xa", 1000), _row("0xb", 5_000_000)],
    )
    snaps = _snapshots(
        FakeClob(settings),
        data_api,
        [
            {"market_id": "0xa", "token_id": "111"},
            {"market_id": "0xb", "token_id": "222"},
        ],
        _RecordingAudit(),
    )
    assert snaps["0xa"]["last_trade_ts"] != snaps["0xb"]["last_trade_ts"]


def test_a_market_with_no_rows_gets_an_honest_empty_tape_ts(tmp_path):
    settings = make_settings(tmp_path)
    data_api = FakeTradesApi(settings, items=[_row("0xa", 1000)])
    snaps = _snapshots(
        FakeClob(settings),
        data_api,
        [
            {"market_id": "0xa", "token_id": "111"},
            {"market_id": "0xempty", "token_id": "222"},
        ],
        _RecordingAudit(),
    )
    assert snaps["0xempty"]["last_trade_ts"] == ""  # never guessed


def test_a_dead_tape_degrades_every_snapshot_but_never_fails_them(tmp_path):
    settings = make_settings(tmp_path)
    data_api = FakeTradesApi(settings, fail=True)
    audit = _RecordingAudit()
    snaps = _snapshots(
        FakeClob(settings),
        data_api,
        [{"market_id": "0xa", "token_id": "111"}],
        audit,
    )
    assert snaps["0xa"]["last_trade_ts"] == ""
    assert snaps["0xa"]["mid_price"] == 0.5
    # recorded as a stage error, but the snapshot still exists
    assert [stage for stage, _ in audit.events] == ["stage_error"]


def test_a_broken_book_drops_only_that_market(tmp_path):
    settings = make_settings(tmp_path)
    data_api = FakeTradesApi(settings, items=[_row("0xa", 1000), _row("0xb", 1000)])

    class HalfBrokenClob(FakeClob):
        def get_orderbook(self, token_id: str) -> dict:
            if token_id == "222":
                raise RuntimeError("clob down for this token")
            return self.book

    snaps = _snapshots(
        HalfBrokenClob(settings),
        data_api,
        [
            {"market_id": "0xa", "token_id": "111"},
            {"market_id": "0xb", "token_id": "222"},
        ],
        _RecordingAudit(),
    )
    assert set(snaps) == {"0xa"}  # the broken book is absent, not fabricated


def test_duplicate_market_ids_collapse_to_one_snapshot(tmp_path):
    settings = make_settings(tmp_path)
    data_api = FakeTradesApi(settings, items=[_row("0xa", 1000)])
    snaps = _snapshots(
        FakeClob(settings),
        data_api,
        [
            {"market_id": "0xa", "token_id": "111"},
            {"market_id": "0xa", "token_id": "111"},
        ],
        _RecordingAudit(),
    )
    assert list(snaps) == ["0xa"]
    assert data_api.calls[0]["condition"] == "0xa"


def test_no_candidates_makes_no_tape_call(tmp_path):
    settings = make_settings(tmp_path)
    data_api = FakeTradesApi(settings)
    assert _snapshots(FakeClob(settings), data_api, [], _RecordingAudit()) == {}
    assert data_api.calls == []


# ------------------------------------------------------- batch position marks
def _position(market_id: str, token_id: str, side: str) -> dict:
    return {
        "market_id": market_id,
        "token_id": token_id,
        "side": side,
        "contracts": 100.0,
        "avg_price": 0.50,
        "current_price": 0.50,
        "unrealized_pnl": 0.0,
        "question": "Q?",
    }


def test_marking_many_positions_uses_one_batch_call(tmp_path):
    settings = make_settings(tmp_path)
    clob = FakeClob(settings)
    calls: list[list[str]] = []
    original = clob.get_midpoints

    def recording_midpoints(token_ids: list[str]) -> dict[str, float]:
        calls.append(list(token_ids))
        return original(token_ids)

    clob.get_midpoints = recording_midpoints  # type: ignore[method-assign]
    positions = [
        _position("m1", "t1", "YES"),
        _position("m2", "t2", "YES"),
        _position("m3", "t3", "NO"),
    ]
    marked = _mark_positions(clob, positions)
    assert len(calls) == 1
    assert calls[0] == ["t1", "t2", "t3"]
    assert [p["current_price"] for p in marked] == [0.5, 0.5, 0.5]


def test_marking_falls_back_per_token_when_the_batch_misses_one(tmp_path):
    settings = make_settings(tmp_path)
    clob = FakeClob(settings)

    def partial_midpoints(token_ids: list[str]) -> dict[str, float]:
        return {"t1": 0.8}  # t2 missing -> per-token fallback

    clob.get_midpoints = partial_midpoints  # type: ignore[method-assign]
    marked = _mark_positions(
        clob, [_position("m1", "t1", "YES"), _position("m2", "t2", "NO")]
    )
    by_token = {p["token_id"]: p for p in marked}
    assert by_token["t1"]["current_price"] == 0.8
    assert by_token["t2"]["current_price"] == 0.5  # from the per-token fallback


def test_marking_keeps_the_last_mark_when_every_fetch_fails(tmp_path):
    settings = make_settings(tmp_path)
    clob = FakeClob(settings, fail=True)
    marked = _mark_positions(clob, [_position("m1", "t1", "YES")])
    assert marked[0]["current_price"] == 0.50  # the prior mark, never fabricated