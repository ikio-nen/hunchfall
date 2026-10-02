"""Price-history features for the predictor (CLOB ``/prices-history``).

The predictor used to derive momentum **only** from the ``/v2/trades`` tape;
``ClobClient.get_prices_history`` existed but had no caller. These tests pin
the new feature math (1h/24h change + realized volatility) and the invariant
that they are **model features, not vetoes** — they may inform direction but
can never force abstention.

Live-checked 2026-10-02: ``/prices-history`` takes ``market`` (the asset id),
not ``token_id`` — sending ``token_id=`` returns a well-formed empty history,
which is why the feature never existed. Point shape is ``{"t", "p"}``.
"""

from __future__ import annotations

from app.predict import price_history_features

HOUR = 3600
DAY = 24 * HOUR


def test_change_1h_and_24h_are_measured_against_the_series():
    history = [
        {"t": 1000, "p": 0.40},
        {"t": 1000 + HOUR, "p": 0.42},
        {"t": 1000 + DAY, "p": 0.50},
    ]
    feats = price_history_features(history, now_ts=1000 + DAY)
    assert feats["last"] == 0.50
    assert feats["change_1h"] == 0.08  # 0.50 - 0.42 (one hour before the last point)
    assert feats["change_24h"] == 0.10  # 0.50 - 0.40
    assert feats["points"] == 3


def test_features_default_to_the_newest_point_not_wall_clock():
    """A stale series still measures the move *within* the series."""
    history = [
        {"t": 1000, "p": 0.30},
        {"t": 1000 + HOUR, "p": 0.35},
        {"t": 1000 + DAY, "p": 0.60},
    ]
    feats = price_history_features(history)
    assert feats["change_1h"] == 0.25  # 0.60 - 0.35
    assert feats["change_24h"] == 0.30  # 0.60 - 0.30


def test_realized_vol_is_the_24h_sample_stdev():
    history = [
        {"t": 1000, "p": 0.40},
        {"t": 1000 + HOUR, "p": 0.60},
        {"t": 1000 + 2 * HOUR, "p": 0.40},
    ]
    feats = price_history_features(history, now_ts=1000 + 2 * HOUR)
    # sample stdev of {0.4, 0.6, 0.4} = 0.115470...
    assert feats["realized_vol"] == 0.11547


def test_keys_are_omitted_when_the_series_is_too_short():
    """Never guessed: a one-point series has no change to report."""
    feats = price_history_features([{"t": 1000, "p": 0.50}], now_ts=1000 + DAY)
    assert feats == {"last": 0.50, "points": 1}


def test_unusable_history_is_an_empty_dict():
    assert price_history_features([]) == {}
    assert price_history_features(None) == {}
    assert price_history_features([{"t": None, "p": 0.5}]) == {}
    assert price_history_features([{"t": 1, "p": "abc"}]) == {}
    # degenerate prices are dropped, not clamped into a fake value
    assert price_history_features([{"t": 1, "p": 0.0}, {"t": 2, "p": 1.0}]) == {}


def test_points_are_sorted_before_measuring():
    """The API is not required to return points in order."""
    history = [
        {"t": 1000 + DAY, "p": 0.50},
        {"t": 1000, "p": 0.40},
        {"t": 1000 + HOUR, "p": 0.42},
    ]
    feats = price_history_features(history, now_ts=1000 + DAY)
    assert feats["last"] == 0.50
    assert feats["change_24h"] == 0.10


def test_alternate_point_keys_are_accepted():
    history = [
        {"timestamp": 1000, "price": 0.40},
        {"timestamp": 1000 + DAY, "price": 0.50},
    ]
    feats = price_history_features(history, now_ts=1000 + DAY)
    assert feats["last"] == 0.50
    assert feats["change_24h"] == 0.10
