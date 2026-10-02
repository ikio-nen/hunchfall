"""Unit tests for the millisecond volume scanner. No network.

Covers: 24h-volume parsing across Gamma field variants, top-N sorting,
minimum-volume filtering, the CLOB token-pair filter, context slimming
(only pipeline fields survive), and fail-open behavior when Gamma raises.
"""

import pytest

from app.scraper.fast_scan import (
    fetch_top_volume_markets,
    has_token_pair,
    slim_market,
    volume_24h,
)


def make_market(i: int, volume: float = 100.0, **overrides) -> dict:
    """Build a realistic-ish Gamma market dict with noise fields."""
    market = {
        "id": f"m{i}",
        "conditionId": f"0x{i:04d}",
        "question": f"Will event {i} happen?",
        "clobTokenIds": '["10", "20"]',
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.6", "0.4"]',
        "endDate": "2026-12-01T00:00:00Z",
        "description": "A very long description. " * 200,
        "image": "https://example.com/img.png",
        "icon": "https://example.com/icon.png",
        "slug": f"will-event-{i}-happen",
        "tags": [{"label": "Politics", "slug": "politics"}],
        "volume24hr": volume,
        "liquidity": "999999",
        "closed": False,
    }
    market.update(overrides)
    return market


class FakeGamma:
    """Duck-typed GammaClient: only list_markets is used."""

    def __init__(self, markets=None, exc=None):
        self._markets = markets or []
        self._exc = exc
        self.calls = []

    def list_markets(self, limit=100, after_cursor=None, order=None, ascending=None):
        self.calls.append(
            {
                "limit": limit,
                "after_cursor": after_cursor,
                "order": order,
                "ascending": ascending,
            }
        )
        if self._exc is not None:
            raise self._exc
        return self._markets, None


# ---- volume parsing -------------------------------------------------------
class TestVolume24h:
    @pytest.mark.parametrize(
        "payload,expected",
        [
            ({"volume24hr": 123.4}, 123.4),
            ({"volume24hr": "123.4"}, 123.4),
            ({"volume24h": 50}, 50.0),
            ({"volumeUsd": 77}, 77.0),
            ({"volume": 11}, 11.0),
            ({}, 0.0),
            ({"volume24hr": None}, 0.0),
            ({"volume24hr": "garbage"}, 0.0),
            ({"volume24hr": "1,234.5"}, 1234.5),
        ],
    )
    def test_variants(self, payload, expected):
        assert volume_24h(payload) == pytest.approx(expected)


# ---- token-pair filter ----------------------------------------------------
class TestHasTokenPair:
    def test_json_string_pair(self):
        assert has_token_pair({"clobTokenIds": '["1","2"]'})

    def test_comma_string_pair(self):
        assert has_token_pair({"clobTokenIds": "1,2"})

    def test_list_pair(self):
        assert has_token_pair({"clobTokenIds": ["1", "2"]})

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"clobTokenIds": '["1"]'},
            {"clobTokenIds": "1"},
            {"clobTokenIds": ["1"]},
            {"clobTokenIds": ""},
        ],
    )
    def test_rejects(self, payload):
        assert not has_token_pair(payload)


# ---- slimming --------------------------------------------------------------
class TestSlimMarket:
    def test_drops_noise_fields(self):
        slim = slim_market(make_market(1))
        for key in ("description", "image", "icon", "slug", "liquidity"):
            assert key not in slim

    def test_keeps_pipeline_fields(self):
        slim = slim_market(make_market(1))
        for key in (
            "id",
            "conditionId",
            "question",
            "clobTokenIds",
            "outcomes",
            "outcomePrices",
            "endDate",
            "closed",
            "tags",
            "volume24hr",
        ):
            assert key in slim

    def test_tags_normalized_to_strings(self):
        slim = slim_market(make_market(1))
        assert slim["tags"] == ["Politics"]
        slim2 = slim_market(make_market(2, tags=["sports", {"slug": "nba"}]))
        assert slim2["tags"] == ["sports", "nba"]

    def test_volume_normalized(self):
        slim = slim_market(make_market(1, volume24hr="250.5"))
        assert slim["volume24hr"] == pytest.approx(250.5)

    def test_explicit_volume_wins(self):
        slim = slim_market(make_market(1), volume=42.0)
        assert slim["volume24hr"] == pytest.approx(42.0)

    def test_much_smaller_than_raw(self):
        import json

        raw, slim = make_market(1), slim_market(make_market(1))
        assert len(json.dumps(slim)) < len(json.dumps(raw)) // 5


# ---- top-N selection --------------------------------------------------------
class TestFetchTopVolumeMarkets:
    def test_sorts_descending_and_caps_top_n(self):
        markets = [make_market(i, volume=v) for i, v in enumerate([10, 50, 30, 90, 70])]
        got = fetch_top_volume_markets(FakeGamma(markets), top_n=3)
        assert [m["id"] for m in got] == ["m3", "m4", "m1"]

    def test_min_volume_filter(self):
        markets = [make_market(i, volume=v) for i, v in enumerate([10, 50, 5])]
        got = fetch_top_volume_markets(FakeGamma(markets), min_volume_24h=20.0)
        assert [m["id"] for m in got] == ["m1"]

    def test_token_pair_filter(self):
        markets = [
            make_market(1, volume=100.0),
            make_market(2, volume=999.0, clobTokenIds='["1"]'),
        ]
        got = fetch_top_volume_markets(FakeGamma(markets))
        assert [m["id"] for m in got] == ["m1"]

    def test_returns_slimmed_dicts(self):
        got = fetch_top_volume_markets(FakeGamma([make_market(1)]))
        assert len(got) == 1
        assert "description" not in got[0]
        assert got[0]["volume24hr"] == pytest.approx(100.0)

    def test_empty_page(self):
        assert fetch_top_volume_markets(FakeGamma([])) == []

    def test_gamma_failure_returns_empty(self):
        got = fetch_top_volume_markets(FakeGamma(exc=RuntimeError("boom")))
        assert got == []

    def test_single_page_call(self):
        gamma = FakeGamma([make_market(1)])
        fetch_top_volume_markets(gamma, limit=100, top_n=5)
        assert gamma.calls == [
            {
                "limit": 100,
                "after_cursor": None,
                "order": "volume24hr",
                "ascending": False,
            }
        ]

    def test_order_param_passthrough(self):
        gamma = FakeGamma([make_market(1)])
        fetch_top_volume_markets(gamma, order=None)
        assert gamma.calls[0]["order"] is None

    def test_top_n_zero(self):
        got = fetch_top_volume_markets(FakeGamma([make_market(1)]), top_n=0)
        assert got == []
