"""Loop tape migration: Data API v1 ``/trades`` -> v2 ``/v2/trades?condition=``.

The loop's snapshot tape was the last live v1 call site; v1 retires
2026-10-24. These tests pin the v2 request (``condition=<conditionId>``,
never ``token_id=``) and the v2 row shapes ``_trade_ts`` must accept.
Zero network: the CLOB book and the tape are offline fakes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.loop import _snapshot, _trade_ts
from tests.fakes import FakeClob, FakeTradesApi, make_settings

#: ``matchTime`` values are ISO-8601 and must be read as UTC.
MATCH_TIME = "2026-09-30T12:00:00Z"
MATCH_TIME_TS = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc).timestamp()

CONDITION_ID = "0xcondition"
TOKEN_ID = "98765"


class _RecordingAudit:
    """Minimal audit sink: records events, never touches SQLite."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event_type: str, payload: dict) -> int:
        self.events.append((event_type, payload))
        return len(self.events)


def test_trade_ts_reads_epoch_seconds_and_millis():
    assert _trade_ts({"timestamp": 1_759_224_000}) == float(1_759_224_000)
    assert _trade_ts({"timestamp": 1_759_224_000_000}) == float(1_759_224_000)


def test_trade_ts_reads_the_v2_match_time_aliases():
    assert _trade_ts({"matchTime": MATCH_TIME}) == MATCH_TIME_TS
    assert _trade_ts({"match_time": MATCH_TIME}) == MATCH_TIME_TS
    assert _trade_ts({"usdc_size": 10.0}) is None  # not a timestamp (USD = size x price)


def test_snapshot_tape_asks_v2_for_the_condition_not_the_token_id(tmp_path):
    settings = make_settings(tmp_path)
    data_api = FakeTradesApi(
        settings,
        items=[{"price": 0.50, "size": 1200.0, "side": "BUY", "matchTime": MATCH_TIME}],
    )
    audit = _RecordingAudit()
    snap = _snapshot(FakeClob(settings), data_api, CONDITION_ID, TOKEN_ID, audit)

    # The v2 filter is the market's condition id; a token id is not accepted.
    assert data_api.calls == [{"condition": CONDITION_ID, "limit": 5}]
    assert snap["last_trade_ts"] == datetime.fromtimestamp(
        MATCH_TIME_TS, tz=timezone.utc
    ).isoformat()
    assert snap["mid_price"] == 0.5
    assert audit.events == []


def test_snapshot_survives_a_dead_tape(tmp_path):
    """The tape is informational: failure degrades to "", never to no snapshot."""
    settings = make_settings(tmp_path)
    data_api = FakeTradesApi(settings, fail=True)
    audit = _RecordingAudit()
    snap = _snapshot(FakeClob(settings), data_api, CONDITION_ID, TOKEN_ID, audit)

    assert snap is not None
    assert snap["last_trade_ts"] == ""
    assert audit.events == []  # a tape failure is tolerated, not a stage error
