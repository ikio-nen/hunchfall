"""Polymarket Data API client (read-only, no auth).

Base: https://data-api.polymarket.com (``POLYMARKET_DATA_API_URL``).

VERIFIED endpoints::

    GET /trades             (rate limit: 200 req / 10s)
    GET /v2/prices-history  (rate limit: 200 req / 10s)
    GET /holders
    GET /oi

Used for the trade tape (feed-freshness staleness), price history,
holder counts, and open interest per market — all market-data inputs to
the snapshot/gate. No trading endpoints here (paper only).

Raises DataApiError naming the endpoint on HTTP failure.
"""

from __future__ import annotations

import logging

import httpx

from app.config import Settings

log = logging.getLogger(__name__)


class DataApiError(RuntimeError):
    """Raised when a Data API request fails; names the endpoint."""


class DataApiClient:
    """Sync read-only client for the Polymarket Data API."""

    def __init__(self, settings: Settings) -> None:
        """Store settings and create a sync httpx client (no auth).

        Args:
            settings: App Settings carrying POLYMARKET_DATA_API_URL.
        """
        self.settings = settings
        self._http = httpx.Client(
            base_url=settings.POLYMARKET_DATA_API_URL, timeout=20.0
        )

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        """GET helper that raises DataApiError naming the endpoint.

        Args:
            path: Path under the Data API base URL.
            params: Query params.

        Raises:
            DataApiError: on any HTTP error, naming the endpoint.
        """
        endpoint = f"{self.settings.POLYMARKET_DATA_API_URL}{path}"
        try:
            resp = self._http.get(path, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise DataApiError(
                f"Data API request failed [{endpoint}]: {exc}"
            ) from exc
        return resp.json()

    def get_trades(
        self,
        token_id: str | None = None,
        market_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Recent trades for a token/market.

        GET /trades — 200/10s.

        Args:
            token_id: CLOB token id (optional).
            market_id: Market id (optional).
            limit: Max trades.

        Returns:
            List of trade dicts (price, size, side, timestamp per API).
        """
        params: dict = {"limit": limit}
        if token_id:
            params["token_id"] = token_id
        if market_id:
            params["market_id"] = market_id
        payload = self._get("/trades", params=params)
        return payload if isinstance(payload, list) else payload.get("data", [])

    def get_prices_history_v2(
        self,
        token_id: str,
        start_ts: int | None = None,
        end_ts: int | None = None,
        interval: str = "1h",
    ) -> list[dict]:
        """Price history (v2) for a token.

        GET /v2/prices-history — 200/10s.

        Args:
            token_id: CLOB token id.
            start_ts: Unix start (seconds), optional.
            end_ts: Unix end (seconds), optional.
            interval: Bucket, e.g. "1h".

        Returns:
            List of history points.
        """
        params: dict = {"token_id": token_id, "interval": interval}
        if start_ts is not None:
            params["startTs"] = start_ts
        if end_ts is not None:
            params["endTs"] = end_ts
        payload = self._get("/v2/prices-history", params=params)
        if isinstance(payload, dict):
            for key in ("history", "data", "prices"):
                if isinstance(payload.get(key), list):
                    return payload[key]
            return []
        return payload if isinstance(payload, list) else []

    def get_holders(self, market_id: str) -> list[dict]:
        """Holder distribution for a market. GET /holders.

        Args:
            market_id: Market id.

        Returns:
            List of holder dicts.
        """
        payload = self._get("/holders", params={"market_id": market_id})
        return payload if isinstance(payload, list) else payload.get("data", [])

    def get_oi(self, market_id: str) -> dict:
        """Open interest for a market. GET /oi.

        Args:
            market_id: Market id.

        Returns:
            OI dict from the API.
        """
        payload = self._get("/oi", params={"market_id": market_id})
        return payload if isinstance(payload, dict) else {}

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._http.close()
