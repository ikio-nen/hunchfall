"""CLOB API client — market-data endpoints ONLY (public, no auth).

VERIFIED endpoints (base: https://clob.polymarket.com)::

    GET /price?token_id=X&side=BUY          (1500 req / 10s)
    GET /midpoint?token_id=X                (1500 req / 10s)
    GET /book?token_id=X                    (1500 req / 10s)
        payload includes min_order_size, tick_size, last_trade_price
    GET /spread?token_id=X
    GET /prices-history                     (1000 req / 10s)
    GET /tick-size?token_id=X
    Batched: GET /books, GET /prices, GET /midpoints  (500 req / 10s)
    General limit: 9000 req / 10s.

PAPER TRADING ONLY — TRADING ENDPOINTS ARE NEVER CALLED. This module
contains no private-key signing, no wallet code, no EIP-712/HMAC, and no
``POST /order`` (or any order-placement/cancel endpoint) exists anywhere
in this codebase. Everything here is a read-only market-data GET.

Rate-limit aware: exponential backoff with jitter on 429/5xx, max 4
retries, then raises ClobError naming the endpoint.
"""

from __future__ import annotations

import logging
import random
import time

import httpx

from app.config import Settings

log = logging.getLogger(__name__)

# Verified rate limits (per 10s window); documented here so call sites stay
# well under them: /book /price /midpoint 1500 each, /prices-history 1000,
# batch (/books /prices /midpoints) 500, general 9000.
_MAX_RETRIES = 4
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ClobError(RuntimeError):
    """Raised when a CLOB request fails; names the endpoint."""


class ClobClient:
    """Sync public-only client for the Polymarket CLOB market-data API.

    NOTE: read-only. No trading endpoints exist in this module — paper
    fills are simulated in ``app/execution/paper.py`` against these public
    prices. There is no wallet, no signing, no ``POST /order`` here.
    """

    def __init__(self, settings: Settings) -> None:
        """Store settings and create a sync httpx client (no auth headers).

        Args:
            settings: App Settings carrying POLYMARKET_CLOB_URL.
        """
        self.settings = settings
        # NOTE: deliberately no auth — read-only public endpoints only.
        self._http = httpx.Client(base_url=settings.POLYMARKET_CLOB_URL, timeout=20.0)

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        """GET with exponential backoff on 429/5xx (max 4 retries).

        Args:
            path: Path under the CLOB base URL.
            params: Query params.

        Raises:
            ClobError: on failure after retries, naming the endpoint.
        """
        endpoint = f"{self.settings.POLYMARKET_CLOB_URL}{path}"
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = self._http.get(path, params=params)
                if resp.status_code in _RETRYABLE_STATUS:
                    raise httpx.HTTPStatusError(
                        f"retryable status {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == _MAX_RETRIES:
                    break
                backoff = (2**attempt) + random.uniform(0, 1)
                log.warning(
                    "CLOB %s attempt %d/%d failed (%s); retrying in %.1fs",
                    endpoint,
                    attempt + 1,
                    _MAX_RETRIES + 1,
                    exc,
                    backoff,
                )
                time.sleep(backoff)
        raise ClobError(f"CLOB API request failed [{endpoint}]: {last_exc}")

    # ---- verified single-token market-data endpoints ----------------------
    def get_price(self, token_id: str, side: str = "BUY") -> float:
        """Executable-side price for a token.

        GET /price?token_id=X&side=BUY — 1500/10s. ``side`` is BUY (what a
        taker pays to buy) or SELL (what a taker receives to sell).

        Args:
            token_id: CLOB token id.
            side: "BUY" | "SELL".

        Returns:
            Price in 0..1.
        """
        payload = self._get("/price", params={"token_id": token_id, "side": side})
        if isinstance(payload, dict):
            for key in ("price", "data"):
                if key in payload:
                    value = payload[key]
                    return float(value if not isinstance(value, dict) else value.get("price", 0.0))
        return float(payload)

    def get_midpoint(self, token_id: str) -> float:
        """Mid price of best bid/ask. GET /midpoint?token_id=X — 1500/10s.

        Args:
            token_id: CLOB token id.

        Returns:
            Mid price in 0..1.
        """
        payload = self._get("/midpoint", params={"token_id": token_id})
        if isinstance(payload, dict):
            value = payload.get("midpoint", payload.get("price", payload.get("data")))
            if isinstance(value, dict):
                value = value.get("midpoint", 0.0)
            return float(value)
        return float(payload)

    def get_orderbook(self, token_id: str) -> dict:
        """Full order book for a token.

        GET /book?token_id=X — 1500/10s. Payload includes ``bids``/``asks``
        plus ``min_order_size``, ``tick_size``, ``last_trade_price``.

        Args:
            token_id: CLOB token id.

        Returns:
            Book dict.
        """
        return self._get("/book", params={"token_id": token_id})

    def get_spread(self, token_id: str) -> dict:
        """Spread payload for a token. GET /spread?token_id=X.

        Args:
            token_id: CLOB token id.

        Returns:
            Spread dict from the API.
        """
        payload = self._get("/spread", params={"token_id": token_id})
        return payload if isinstance(payload, dict) else {}

    def get_tick_size(self, token_id: str) -> float:
        """Minimum price increment for a token. GET /tick-size?token_id=X.

        Args:
            token_id: CLOB token id.

        Returns:
            Tick size float.
        """
        payload = self._get("/tick-size", params={"token_id": token_id})
        if isinstance(payload, dict):
            return float(payload.get("tick_size", payload.get("tickSize", 0.0)))
        return float(payload)

    def get_prices_history(
        self,
        token_id: str,
        start_ts: int | None = None,
        end_ts: int | None = None,
        interval: str = "1h",
        fidelity: int | None = None,
    ) -> list[dict]:
        """Price history for a token.

        GET /prices-history — 1000/10s.

        Args:
            token_id: CLOB token id.
            start_ts: Unix start (seconds), optional.
            end_ts: Unix end (seconds), optional.
            interval: Bucket, e.g. "1h".
            fidelity: Fidelity hint (minutes), optional.

        Returns:
            List of history points.
        """
        params: dict = {"token_id": token_id, "interval": interval}
        if start_ts is not None:
            params["startTs"] = start_ts
        if end_ts is not None:
            params["endTs"] = end_ts
        if fidelity is not None:
            params["fidelity"] = fidelity
        payload = self._get("/prices-history", params=params)
        if isinstance(payload, dict):
            for key in ("history", "data", "prices"):
                if isinstance(payload.get(key), list):
                    return payload[key]
            return []
        return payload if isinstance(payload, list) else []

    # ---- derived book math (unchanged from book payload) -------------------
    def _book_sides(self, token_id: str) -> tuple[list, list]:
        """Return (bids, asks) from the order book.

        Args:
            token_id: CLOB token id.

        Returns:
            Tuple of (bids, asks) price/size lists.
        """
        book = self.get_orderbook(token_id)
        return book.get("bids", []), book.get("asks", [])

    @staticmethod
    def _level_price(level: list) -> float | None:
        """Extract float price from a [price, size] level."""
        if not level:
            return None
        try:
            return float(level[0])
        except (TypeError, ValueError, IndexError):
            return None

    def get_mid_price(self, token_id: str) -> float:
        """Mid price of the best bid/ask (derived from the book).

        Args:
            token_id: CLOB token id.

        Returns:
            Mid price in 0..1.

        Raises:
            ClobError: if the book has no usable best bid/ask.
        """
        bids, asks = self._book_sides(token_id)
        best_bid = self._level_price(bids[0]) if bids else None
        best_ask = self._level_price(asks[0]) if asks else None
        if best_bid is None or best_ask is None:
            raise ClobError(
                f"CLOB order book empty for token {token_id}: no mid price"
            )
        return (best_bid + best_ask) / 2.0

    def get_spread_bps(self, token_id: str) -> float:
        """Bid-ask spread in basis points relative to mid (from the book).

        Args:
            token_id: CLOB token id.

        Returns:
            Spread in bps; inf if mid is zero.
        """
        bids, asks = self._book_sides(token_id)
        best_bid = self._level_price(bids[0]) if bids else None
        best_ask = self._level_price(asks[0]) if asks else None
        if best_bid is None or best_ask is None:
            raise ClobError(
                f"CLOB order book empty for token {token_id}: no spread"
            )
        mid = (best_bid + best_ask) / 2.0
        if mid <= 0:
            return float("inf")
        return (best_ask - best_bid) / mid * 10_000.0

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._http.close()
