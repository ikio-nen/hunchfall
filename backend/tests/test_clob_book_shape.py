"""CLOB book shape and ordering — live-checked, previously guessed wrong.

Live-checked 2026-10-01 on a market trading at 2.85¢ (the ``REAL_BOOK`` below
is that payload, trimmed to three levels a side):

* levels are **objects with string numbers** —
  ``{"price": "0.001", "size": "12142623.48"}`` — not ``[price, size]`` pairs,
  so pair indexing raised ``KeyError: 0`` on the real feed;
* **bids ascend and asks descend**, so index 0 is the *worst* quote: reading
  it produced a **50.0¢ mid and a 99.8¢ spread for a 2.85¢ market**, which the
  gate would veto and the predictor would report as the market's base rate.

``clob.book_levels`` normalizes both, so every consumer (predictor, loop,
extension scan, paper fill walk) sees best-first ``(price, size)`` pairs.
"""

from __future__ import annotations

import pytest

from app.execution.paper import PaperEngine
from app.polymarket.clob import ClobClient, book_levels, parse_level
from app.policy.gate import Signal
from app.predict import book_features

#: The real ``/book`` payload for a 2.85¢ market, trimmed to 3 levels a side.
REAL_BOOK = {
    "bids": [
        {"price": "0.001", "size": "12142623.48"},
        {"price": "0.026", "size": "5000.0"},
        {"price": "0.028", "size": "6155.24"},
    ],
    "asks": [
        {"price": "0.999", "size": "2178005.12"},
        {"price": "0.032", "size": "3000.0"},
        {"price": "0.029", "size": "4200.5"},
    ],
    "last_trade_price": "0.028",
    "min_order_size": "5",
    "tick_size": "0.001",
}


def _signal(**overrides) -> Signal:
    """A minimal Signal for the book-walk helpers."""
    base = dict(
        market_id="m1",
        token_id="t1",
        question="Will it rain tomorrow?",
        p_true=0.70,
        market_price=0.50,
        spread_cents=0.0,
        top_book_depth_usd=100_000.0,
        hours_to_resolution=48.0,
        news_ts="2026-09-30T00:00:00+00:00",
        price_ts="2026-09-30T00:10:00+00:00",
        negrisk_sum=1.0,
        uma_dispute=False,
        jev_confidence=0.8,
        jev_choice="YES",
        fee_category="Other",
        book_asks=[],
        book_bids=[],
    )
    base.update(overrides)
    return Signal(**base)


# ------------------------------------------------------------- level parsing
def test_parse_level_handles_the_real_object_with_string_numbers():
    assert parse_level({"price": "0.001", "size": "12142623.48"}) == (
        0.001,
        12142623.48,
    )


def test_parse_level_keeps_accepting_pairs_and_rejects_junk():
    assert parse_level([0.49, 5000.0]) == (0.49, 5000.0)
    assert parse_level((0.49, 5000.0)) == (0.49, 5000.0)
    assert parse_level({}) is None
    assert parse_level({"price": "abc", "size": "1"}) is None
    assert parse_level([]) is None
    assert parse_level("0.49") is None


# ----------------------------------------------------------- best-first order
def test_book_levels_returns_the_true_touch_not_index_zero():
    bids, asks = book_levels(REAL_BOOK)
    assert bids[0] == (0.028, 6155.24)  # best bid = highest, served LAST
    assert asks[0] == (0.029, 4200.5)  # best ask = lowest, served LAST
    assert bids == [(0.028, 6155.24), (0.026, 5000.0), (0.001, 12142623.48)]
    assert asks == [(0.029, 4200.5), (0.032, 3000.0), (0.999, 2178005.12)]


def test_index_zero_would_have_been_a_fifty_cent_market():
    """Documents the bug this normalization removed."""
    naive_bid = float(REAL_BOOK["bids"][0]["price"])
    naive_ask = float(REAL_BOOK["asks"][0]["price"])
    assert (naive_bid + naive_ask) / 2.0 == 0.5  # 50.0¢ for a 2.85¢ market
    bids, asks = book_levels(REAL_BOOK)
    assert (bids[0][0] + asks[0][0]) / 2.0 == 0.0285  # the truth


def test_book_levels_limits_each_side():
    bids, asks = book_levels(REAL_BOOK, 2)
    assert len(bids) == 2 and len(asks) == 2
    assert bids[0] == (0.028, 6155.24)  # keeps the BEST levels, not the first


def test_book_levels_handles_a_missing_or_empty_book():
    assert book_levels({}) == ([], [])
    assert book_levels({"bids": [], "asks": []}) == ([], [])


# ------------------------------------------------------------ consumers
def test_book_features_reads_the_real_touch():
    features = book_features(REAL_BOOK, 5)
    assert features["market_mid"] == 0.0285
    assert features["best_bid"] == 0.028
    assert features["best_ask"] == 0.029
    assert features["spread_cents"] == 0.1
    assert features["bid_depth_usd"] < features["ask_depth_usd"]
    assert features["last_trade_price"] == 0.028


def test_clob_level_price_accepts_the_object_shape():
    assert ClobClient._level_price({"price": "0.028", "size": "1"}) == 0.028
    assert ClobClient._level_price([0.028, 1.0]) == 0.028
    assert ClobClient._level_price({"size": "1"}) is None


def test_yes_walk_takes_the_best_ask_first():
    signal = _signal(book_asks=[(0.029, 100.0), (0.05, 100.0)])
    assert PaperEngine._take_levels(signal, "YES") == [(0.029, 100.0), (0.05, 100.0)]


def test_no_walk_mirrors_the_best_bid_first():
    """book_bids arrive best-first, so mirrored NO asks must too (no reversal)."""
    signal = _signal(book_bids=[(0.10, 100.0), (0.05, 100.0)])
    levels = PaperEngine._take_levels(signal, "NO")
    assert [price for price, _ in levels] == pytest.approx([0.90, 0.95])
    assert [size for _, size in levels] == [100.0, 100.0]
