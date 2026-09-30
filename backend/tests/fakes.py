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
        "clobTokenIds": "111,222",
        "endDate": (now + timedelta(hours=48)).isoformat(),
        "volume": 12345,
        "closed": False,
        "tags": [],
    }
    market.update(overrides)
    return market


def trade_items() -> list[dict]:
    """Five-ish recent TRADE activity items (Data API v2 shape)."""
    now = datetime.now(timezone.utc).timestamp()
    return [
        {
            "side": "BUY",
            "size": 600.0,
            "price": 0.5,
            "timestamp": now - 30,
            "title": "Will it rain tomorrow?",
        },
        {
            "side": "BUY",
            "size": 300.0,
            "price": 0.51,
            "timestamp": now - 20,
            "title": "Will it rain tomorrow?",
        },
        {
            "side": "SELL",
            "size": 400.0,
            "price": 0.49,
            "timestamp": now - 10,
            "title": "Will it rain tomorrow?",
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
    ) -> None:
        self.settings = settings
        self.book = (
            book
            if book is not None
            else {"bids": [[0.49, 5000.0]], "asks": [[0.51, 5000.0]]}
        )
        self.fail = fail

    def get_orderbook(self, token_id: str) -> dict:
        if self.fail:
            raise RuntimeError("clob down")
        return self.book

    def get_mid_price(self, token_id: str) -> float:
        if self.fail:
            raise RuntimeError("clob down")
        bids = self.book.get("bids") or []
        asks = self.book.get("asks") or []
        if not bids or not asks:
            raise RuntimeError("empty book")
        return (float(bids[0][0]) + float(asks[0][0])) / 2.0

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


def patch_scan(monkeypatch, *, gamma=None, clob=None, data_api=None, jev=None) -> None:
    """Point ``app.scan`` at the offline fakes."""
    monkeypatch.setattr("app.scan.GammaClient", gamma or FakeGamma)
    monkeypatch.setattr("app.scan.ClobClient", clob or FakeClob)
    monkeypatch.setattr("app.scan.DataApiClient", data_api or FakeDataApi)
    monkeypatch.setattr("app.scan.JevClient", jev or FakeJev)


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
