"""RFC-003 feature math + decision policy — hand-computed, zero network.

The tape assertions pin the **USD = ``size × price``** rule from RFC-003 §3:
``/v2/trades`` rows carry no ``usdc_size``, and one is ignored if it appears.
"""

from __future__ import annotations

import pytest

from app.predict import (
    PredictUpstreamError,
    book_features,
    build_reasons,
    build_snapshot_text,
    decide,
    hours_to_resolution,
    tape_features,
)
from tests.fakes import mixed_trade_v2_items, trade_v2_items


def test_book_features_depth_and_imbalance():
    book = {"bids": [[0.49, 5000.0]], "asks": [[0.51, 5000.0]]}
    features = book_features(book, levels=5)
    assert features["market_mid"] == 0.5
    assert features["best_bid"] == 0.49
    assert features["best_ask"] == 0.51
    assert features["spread_cents"] == 2.0
    assert features["bid_depth_usd"] == 2450.0  # 0.49 * 5000
    assert features["ask_depth_usd"] == 2550.0  # 0.51 * 5000
    assert features["imbalance"] == -0.02  # (2450 - 2550) / 5000


def test_book_features_empty_side_is_upstream_error():
    with pytest.raises(PredictUpstreamError) as exc:
        book_features({"bids": [], "asks": [[0.51, 100.0]]}, levels=5)
    assert exc.value.source == "clob"
    assert exc.value.status == 502


def test_tape_usd_is_size_times_price():
    tape = tape_features(trade_v2_items())
    assert tape["count"] == 3
    # buy = 1200*0.50 + 600*0.51 ; sell = 800*0.49
    assert tape["buy_usd"] == 906.0
    assert tape["sell_usd"] == 392.0
    assert tape["flow_imbalance"] == 0.396  # (906-392)/1298
    assert tape["vwap"] == 0.499231  # 1298 / 2600


def test_tape_ignores_usdc_size_when_present():
    rows = [
        {
            "price": 0.5,
            "size": 100.0,
            "side": "BUY",
            "timestamp": 1_759_224_000,
            "usdc_size": 999_999.0,  # must be ignored: USD = size * price
        }
    ]
    tape = tape_features(rows, now_ts=1_759_224_100)
    assert tape["buy_usd"] == 50.0


def test_tape_windows_momentum_and_accel():
    now = 2_000_000_000.0
    rows = [
        {"price": 0.60, "size": 100.0, "side": "BUY", "timestamp": now - 60},
        {"price": 0.40, "size": 100.0, "side": "BUY", "timestamp": now - 1200},
    ]
    tape = tape_features(rows, now_ts=now)
    assert tape["momentum_15m"] == 0.2  # vwap(last 15m) 0.60 - vwap(15-30m) 0.40
    assert "momentum_60m" not in tape  # prior 60-120m window is empty -> omitted
    # last 15m usd = 60.0 ; baseline = 40.0 / 8 = 5.0
    assert tape["volume_accel"] == 12.0


def test_tape_empty_is_zero_state():
    tape = tape_features([], now_ts=2_000_000_000.0)
    assert tape["count"] == 0
    assert tape["buy_usd"] == 0.0
    assert tape["flow_imbalance"] == 0.0
    assert "vwap" not in tape


def test_decide_yes_no_and_boundary():
    yes = decide(0.67, 0.55, 0.10, 0.0)
    assert yes["edge_vs_market"] == 0.12
    assert yes["direction"] == "YES"
    assert yes["abstained"] is False
    assert yes["confidence"] == 0.34  # |2*0.67 - 1| = 0.34

    no = decide(0.40, 0.55, 0.10, 0.0)
    assert no["edge_vs_market"] == -0.15
    assert no["direction"] == "NO"

    # exactly at the threshold -> a guess (>=), just below -> abstain
    assert decide(0.65, 0.55, 0.10, 0.0)["direction"] == "YES"
    assert decide(0.649, 0.55, 0.10, 0.0)["abstained"] is True


def test_decide_confidence_is_discounted_by_disagreement():
    # |2*0.9 - 1| = 0.8 ; disagreement 0.5 -> 0.8 * (1 - 0.5) = 0.4
    assert decide(0.9, 0.5, 0.10, 0.5)["confidence"] == 0.4


def test_hours_to_resolution_local_math():
    market = {"endDate": "2026-12-31T23:59:59Z"}
    hours = hours_to_resolution(market, now_ts=1_798_761_599.0)  # 2026-12-31T23:59:59Z
    assert hours is not None and abs(hours) < 0.001
    assert hours_to_resolution({"endDate": "not-a-date"}) is None


def test_reasons_always_nonempty_and_typed_on_abstention():
    features = {"market_mid": 0.55, "imbalance": 0.1474}
    decision = decide(0.55, 0.55, 0.10, 0.0)
    reasons = build_reasons(features, decision, ["tape"], 0.10)
    assert reasons  # never empty
    assert any("abstained" in reason for reason in reasons)
    assert any("missing feature source: tape" in reason for reason in reasons)


def test_social_counts_are_labelled_network_wide_and_unfiltered():
    """The Jetstream tail is unfiltered, so the counts must say so.

    Regression guard for the honesty-of-display fix: the public tail counts
    every Bluesky post in the window, not the market's subject, so no surface
    may present them as the subject's own social pulse.
    """
    features = {
        "market_mid": 0.55,
        "imbalance": 0.1474,
        "social": {
            "relevance": "direct",
            "proxy": False,
            "platforms_ok": ["bluesky"],
            "posts_window": 41,
            "engagement_window": 12,
        },
    }
    decision = decide(0.67, 0.55, 0.10, 0.0)
    reasons = build_reasons(features, decision, [], 0.10)
    assert any("network-wide (unfiltered)" in reason for reason in reasons)
    assert "network-wide (unfiltered)" in build_snapshot_text(
        "Will it rain tomorrow?", 0.55, features, []
    )


def test_tape_ignores_the_other_token():
    """Regression: ``/v2/trades`` interleaves BOTH outcomes.

    Live-checked 2026-10-01: 71 of the last 100 rows for one market were
    NO-token trades near 0.97 while the YES token traded at 0.029, so the
    unfiltered vwap read 0.7628 for a 2.85c market and the "buy" flow was
    really the NO side's.
    """
    mixed = mixed_trade_v2_items()
    filtered = tape_features(mixed, yes_token_id="111")
    yes_only = tape_features(trade_v2_items(), yes_token_id="111")
    assert filtered == yes_only  # the NO-token rows contribute nothing
    assert filtered["count"] == 3
    # and the unfiltered math really is wrong on the same rows
    assert tape_features(mixed)["vwap"] != filtered["vwap"]


def test_tape_drops_rows_that_identify_no_side():
    unscoped = [{"price": 0.5, "size": 10.0, "side": "BUY", "timestamp": 1}]
    assert tape_features(unscoped, yes_token_id="111")["count"] == 0
    assert tape_features(unscoped)["count"] == 1  # unit tests can opt out


def test_tape_accepts_outcome_aliases_when_token_id_is_absent():
    rows = [
        {"price": 0.5, "size": 10.0, "side": "BUY", "timestamp": 1, "outcome_index": 0},
        {"price": 0.97, "size": 10.0, "side": "BUY", "timestamp": 1, "outcome_index": 1},
        {"price": 0.5, "size": 10.0, "side": "BUY", "timestamp": 1, "outcome": "Yes"},
    ]
    tape = tape_features(rows, yes_token_id="111")
    # outcome_index 0 and outcome "Yes" are kept; outcome_index 1 is not
    assert tape["count"] == 2


def test_snapshot_text_is_bounded_and_mentions_the_base_rate():
    text = build_snapshot_text(
        "Will it rain tomorrow?", 0.55, {"market_mid": 0.55, "imbalance": 0.1}, []
    )
    assert "Market: Will it rain tomorrow?" in text
    assert "base rate" in text
    assert len(text) <= 2048
