"""Shared offline fakes for the RFC-001 API tests — zero network.

Every fake mirrors the public surface of the real client it replaces, so
tests exercise the real service logic (gate, persistence, screening,
normalization) while network calls are impossible.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import Settings
from app.jev.client import Decision
from app.polymarket.data_api import DataApiError
from app.polymarket.clob import book_levels
from app.polymarket.gamma import GammaError


def make_settings(tmp_path, **overrides) -> Settings:
    """Settings with an absolute temp DB and activity caching disabled."""
    base = dict(
        DATABASE_PATH=str(tmp_path / "hunchfall-test.db"),
        JEV_MOCK=True,
        WALLET_ACTIVITY_TTL_SEC=0,
        PAPER_BANKROLL_USD=10000.0,
    )
    base.update(overrides)
    return Settings(**base)


def market_payload(**overrides) -> dict:
    """A Gamma-shaped market payload: 48h horizon, two-sided token pair."""
    now = datetime.now(timezone.utc)
    market = {
        "id": "123",
        "conditionId": "0xcondition",
        "slug": "will-it-rain-tomorrow",
        "question": "Will it rain tomorrow?",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.5", "0.5"]',
        # REAL shape (live-checked 2026-10-01): a JSON-array string, not a
        # bare comma-separated list. A fixture that mirrored the old guess let
        # a broken parser pass the whole suite while the live API 400'd.
        "clobTokenIds": '["111", "222"]',
        "endDate": (now + timedelta(hours=48)).isoformat(),
        "volume": 12345,
        "closed": False,
        "tags": [],
    }
    market.update(overrides)
    return market


def trade_items() -> list[dict]:
    """Recent TRADE activity items in the *real* Data API v2 (snake_case) shape.

    ``usdc_size`` is the USD notional, ``size`` is shares. They differ on
    purpose: a normalizer that prefers ``size`` reports shares as USD, so the
    volume assertions in ``test_api_scan.py`` fail if that regresses.
    """
    now = datetime.now(timezone.utc).timestamp()
    return [
        {
            "side": "BUY",
            "size": 1200.0,
            "usdc_size": 600.0,
            "price": 0.5,
            "timestamp": now - 30,
            "title": "Will it rain tomorrow?",
            "transaction_hash": "0x" + "1a" * 32,
        },
        {
            "side": "BUY",
            "size": 600.0,
            "usdc_size": 300.0,
            "price": 0.51,
            "timestamp": now - 20,
            "title": "Will it rain tomorrow?",
            "transaction_hash": "0x" + "2b" * 32,
        },
        {
            "side": "SELL",
            "size": 800.0,
            "usdc_size": 400.0,
            "price": 0.49,
            "timestamp": now - 10,
            "title": "Will it rain tomorrow?",
            "transaction_hash": "0x" + "3c" * 32,
        },
    ]


class FakeGamma:
    """Gamma stub (not-found behavior configurable)."""

    def __init__(
        self,
        settings: Settings,
        market: dict | None = None,
        not_found: bool = False,
    ) -> None:
        self.settings = settings
        self.market = market if market is not None else market_payload()
        self.not_found = not_found

    def get_market_by_slug(self, slug: str) -> dict:
        if self.not_found:
            raise GammaError(
                f"Gamma API request failed [GET /markets/slug/{slug}]: 404 Not Found"
            )
        return dict(self.market)

    def get_market_by_condition_id(self, condition_id: str) -> dict:
        """Condition-id lookup: the real API returns an empty list when
        nothing matches, which callers map to not-found."""
        if self.not_found:
            return {}
        return dict(self.market)

    def get_event_by_slug(self, slug: str) -> dict:
        if self.not_found:
            raise GammaError(
                f"Gamma API request failed [GET /events/slug/{slug}]: 404 Not Found"
            )
        return {"slug": slug, "title": "Event", "markets": [dict(self.market)]}

    def close(self) -> None:
        pass


class FakeClob:
    """CLOB stub returning a fixed book (or failing)."""

    def __init__(
        self,
        settings: Settings,
        book: dict | None = None,
        fail: bool = False,
        history: list[dict] | None = None,
    ) -> None:
        self.settings = settings
        # REAL shape and order (live-checked 2026-10-01): level OBJECTS with
        # STRING numbers, bids ascending and asks descending — the touch is the
        # LAST element on each side, so a fixture that only ever had one level
        # per side hid both bugs. The outer levels are deliberately asymmetric:
        # reading index 0 would give mid 0.525 instead of the true 0.50.
        self.book = (
            book
            if book is not None
            else {
                "bids": [
                    {"price": "0.35", "size": "1000.0"},
                    {"price": "0.49", "size": "5000.0"},
                ],
                "asks": [
                    {"price": "0.70", "size": "1000.0"},
                    {"price": "0.51", "size": "5000.0"},
                ],
                "last_trade_price": "0.50",
                "min_order_size": "5",
                "tick_size": "0.001",
            }
        )
        self.fail = fail
        self.token_ids: list[str] = []
        self.history = history

    def get_orderbook(self, token_id: str) -> dict:
        self.token_ids.append(token_id)
        if self.fail:
            raise RuntimeError("clob down")
        return self.book

    def get_mid_price(self, token_id: str) -> float:
        if self.fail:
            raise RuntimeError("clob down")
        bids, asks = book_levels(self.book)
        if not bids or not asks:
            raise RuntimeError("empty book")
        return (bids[0][0] + asks[0][0]) / 2.0

    def get_midpoints(self, token_ids: list[str]) -> dict[str, float]:
        """Batch mids: one per token, or empty when this fake is failing."""
        if self.fail:
            return {}
        return {str(t): self.get_mid_price(str(t)) for t in token_ids}

    def get_prices(self, token_ids: list[str], side: str = "BUY") -> dict[str, float]:
        """Batch prices: mirror of the mid (the fake book is symmetric)."""
        return self.get_midpoints(token_ids)

    def get_prices_history(
        self, token_id: str, **kwargs: Any
    ) -> list[dict]:
        """Canned history (overridable via the ``history`` kwarg).

        Default is a flat 24-point hourly series at the book mid, so the
        price-history features are computable but move-free.
        """
        if self.fail:
            raise RuntimeError("clob down")
        if self.history is not None:
            return [dict(p) for p in self.history]
        mid = self.get_mid_price(token_id)
        now = int(datetime.now(timezone.utc).timestamp())
        return [{"t": now - 3600 * i, "p": mid} for i in range(24, 0, -1)]

    def close(self) -> None:
        pass


class FakeDataApi:
    """Data API stub serving the v2 activity tape."""

    def __init__(
        self,
        settings: Settings,
        items: list[dict] | None = None,
        fail: bool = False,
    ) -> None:
        self.settings = settings
        self.items = items if items is not None else trade_items()
        self.fail = fail

    def get_activity_v2(self, **kwargs: Any) -> list[dict]:
        if self.fail:
            raise DataApiError("data-api down")
        return [dict(item) for item in self.items]

    def close(self) -> None:
        pass


class FakeJev:
    """Jev stub returning a fixed typed decision."""

    def __init__(
        self,
        settings: Settings,
        p_true: float = 0.8,
        choice: str = "YES",
        confidence: float = 0.8,
        mock: bool = False,
    ) -> None:
        self.settings = settings
        self.p_true = p_true
        self.choice = choice
        self.confidence = confidence
        self.mock = mock

    def decide(self, state: Any) -> Decision:
        return Decision(
            p_true=self.p_true,
            choice=self.choice,
            signal_strength="strong",
            signal_score=1.0,
            jev_model="fake-1.0",
            jev_confidence=self.confidence,
            jev_choice=self.choice,
            raw={"fake": True},
            mock=self.mock,
        )

    def close(self) -> None:
        pass


def _trade_v2_row(
    price: float,
    size: float,
    side: str,
    ts: float,
    *,
    token_id: str = "111",
    outcome: str = "Yes",
    outcome_index: int = 0,
) -> dict:
    """One row shaped like the real ``/v2/trades`` payload (live 2026-10-01)."""
    return {
        "proxy_wallet": "0xabc",
        "condition_id": "0xcondition",
        "token_id": token_id,
        "outcome": outcome,
        "outcome_index": outcome_index,
        "side": side,
        "price": price,
        "size": size,
        "timestamp": int(ts),
        "title": "Will it rain tomorrow?",
        "slug": "will-it-rain-tomorrow",
        "transaction_hash": "0xtx",
    }


def trade_v2_items() -> list[dict]:
    """YES-token rows in the *real* ``/v2/trades`` shape.

    These rows carry **no ``usdc_size``** — USD notional is ``size × price``
    (RFC-003 §3). Hand-computed expectations for this fixture:
    buy = 1200*0.50 + 600*0.51 = 906.0, sell = 800*0.49 = 392.0,
    vwap = 1298/2600 = 0.499231.
    """
    now = datetime.now(timezone.utc).timestamp()
    return [
        _trade_v2_row(0.50, 1200.0, "BUY", now - 60),
        _trade_v2_row(0.51, 600.0, "BUY", now - 30),
        _trade_v2_row(0.49, 800.0, "SELL", now - 10),
    ]


def mixed_trade_v2_items() -> list[dict]:
    """What the endpoint really returns: **both tokens interleaved**.

    Live-checked 2026-10-01 — 71 of the last 100 rows for one market were
    NO-token trades priced near the complement (~0.97) while the YES token
    traded at ~0.029. Any feature that forgets to scope to one token is
    obviously wrong on this fixture (mixed vwap lands near 0.76).
    """
    now = datetime.now(timezone.utc).timestamp()
    no_rows = [
        _trade_v2_row(
            0.97, 3000.0, "BUY", now - 50, token_id="222", outcome="No", outcome_index=1
        ),
        _trade_v2_row(
            0.96, 500.0, "SELL", now - 20, token_id="222", outcome="No", outcome_index=1
        ),
    ]
    return trade_v2_items() + no_rows


class FakeTradesApi:
    """Data API stub serving the ``/v2/trades`` predictor tape."""

    def __init__(
        self,
        settings: Settings,
        items: list[dict] | None = None,
        fail: bool = False,
    ) -> None:
        self.settings = settings
        # Default to the REAL mixed feed, so the API paths exercise the
        # YES-token scoping instead of a fixture that hides the bug.
        self.items = items if items is not None else mixed_trade_v2_items()
        self.fail = fail
        self.calls: list[dict] = []

    def get_trades_v2(
        self, condition: str, limit: int = 100, cursor=None, **kwargs: Any
    ) -> list[dict]:
        self.calls.append({"condition": condition, "limit": limit})
        if self.fail:
            raise DataApiError("data-api down")
        return [dict(item) for item in self.items]

    def get_trades_v2_many(
        self, conditions: list[str], limit: int = 100
    ) -> dict[str, list[dict]]:
        """Batched tape: one call, rows split by their own ``condition_id``.

        Mirrors the real client's contract: every requested id is a key, a row
        is filed under its own ``condition_id``, and unrequested rows are
        dropped.
        """
        wanted = [str(c) for c in conditions if str(c)]
        self.calls.append({"condition": ",".join(wanted), "limit": limit})
        if self.fail:
            raise DataApiError("data-api down")
        out: dict[str, list[dict]] = {c: [] for c in wanted}
        allowed = set(wanted)
        for item in self.items:
            key = str(item.get("condition_id") or "")
            if key in allowed:
                out[key].append(dict(item))
        return out

    def close(self) -> None:
        pass


def patch_scan(monkeypatch, *, gamma=None, clob=None, data_api=None, jev=None) -> None:
    """Point ``app.scan`` at the offline fakes."""
    monkeypatch.setattr("app.scan.GammaClient", gamma or FakeGamma)
    monkeypatch.setattr("app.scan.ClobClient", clob or FakeClob)
    monkeypatch.setattr("app.scan.DataApiClient", data_api or FakeDataApi)
    monkeypatch.setattr("app.scan.JevClient", jev or FakeJev)


def patch_predict(monkeypatch, *, gamma=None, clob=None, data_api=None, jev=None) -> None:
    """Point ``app.predict`` at the offline fakes."""
    monkeypatch.setattr("app.predict.GammaClient", gamma or FakeGamma)
    monkeypatch.setattr("app.predict.ClobClient", clob or FakeClob)
    monkeypatch.setattr("app.predict.DataApiClient", data_api or FakeTradesApi)
    monkeypatch.setattr("app.predict.JevClient", jev or FakeJev)


def patch_social(
    monkeypatch, *, jetstream=None, reddit=None, rss=None
) -> None:
    """Point ``app.social`` collectors at offline fakes (never touch network)."""
    monkeypatch.setattr("app.social.collect_jetstream", jetstream or (lambda s, t, to: None))
    monkeypatch.setattr("app.social.collect_reddit", reddit or (lambda s, t, to: None))
    monkeypatch.setattr("app.social.collect_rss", rss or (lambda s, t, to: None))


def predict_body(**overrides) -> dict:
    """A valid ``POST /predict`` request body."""
    body = {"market_slug": "will-it-rain-tomorrow"}
    body.update(overrides)
    return body


def scan_body(**overrides) -> dict:
    """A valid ``/extension/scan`` request body."""
    body = {
        "marketplace": "polymarket",
        "kind": "market",
        "slug": "will-it-rain-tomorrow",
        "url": "https://polymarket.com/market/will-it-rain-tomorrow",
        "page_state": {
            "yesPrice": 0.99,
            "noPrice": 0.01,
            "title": "Will it rain tomorrow?",
        },
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }
    body.update(overrides)
    return body
