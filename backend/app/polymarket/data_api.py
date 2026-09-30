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

Additive v2 methods (``get_activity_v2`` / ``get_positions_v2``) follow
the verified Data API v2 contract: ``{"data", "pagination"}`` envelope,
NO ``offset`` param, cursor pagination, 429 + ``Retry-After`` retries,
and an empty ``data`` array as a valid zero-state. v1 retires 2026-10-24.

Raises DataApiError naming the endpoint on HTTP failure.
"""

from __future__ import annotations

import logging
import time

import httpx

from app.config import Settings

log = logging.getLogger(__name__)


class DataApiError(RuntimeError):
    """Raised when a Data API request fails; names the endpoint."""


def _retry_after_seconds(raw: str | None) -> float:
    """Parse a 429 ``Retry-After`` header; default 1s when unreadable.

    Args:
        raw: Header value (seconds or an HTTP date).

    Returns:
        Non-negative seconds to wait.
    """
    if not raw:
        return 1.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 1.0


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

    # ---- Data API v2 (verified params; v1 retires 2026-10-24) -------------
    def _get_v2(self, path: str, params: dict | None = None) -> tuple[list, dict]:
        """GET a v2 route and unwrap the ``{data, pagination}`` envelope.

        Never sends ``offset`` (v2 rejects it) and honors ``Retry-After``
        on 429 up to two retries (sleep capped at 5s).

        Args:
            path: v2 path, e.g. "/v2/activity".
            params: Query params.

        Returns:
            ``(data_list, pagination_dict)``.

        Raises:
            DataApiError: on HTTP failure after retries.
        """
        endpoint = f"{self.settings.POLYMARKET_DATA_API_URL}{path}"
        clean = {
            key: value
            for key, value in (params or {}).items()
            if key != "offset" and value is not None
        }
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = self._http.get(path, params=clean)
                if resp.status_code == 429:
                    if attempt >= 2:
                        break
                    retry_after = _retry_after_seconds(resp.headers.get("Retry-After"))
                    time.sleep(min(retry_after, 5.0))
                    continue
                resp.raise_for_status()
                payload = resp.json()
                if isinstance(payload, dict):
                    data = payload.get("data", [])
                    pagination = payload.get("pagination") or {}
                    return (data if isinstance(data, list) else []), pagination
                return (payload if isinstance(payload, list) else []), {}
            except httpx.HTTPError as exc:
                last_exc = exc
                break
        raise DataApiError(f"Data API v2 request failed [{endpoint}]: {last_exc}")

    def get_activity_v2(
        self,
        user: str | None = None,
        condition: str | None = None,
        activity_type: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        sort_direction: str | None = "DESC",
        exclude_deposits_withdrawals: bool | None = True,
    ) -> list[dict]:
        """Wallet activity / trade tape feed (verified v2 params).

        GET /v2/activity. ``user`` anchors the feed; ``condition`` selects a
        market. ``type=TRADE`` gives the trade tape used by the extension
        scan. An empty list is a valid zero-state, not an error.

        Args:
            user: EVM address (wallet feed).
            condition: Condition id (market selection).
            activity_type: Comma-separated types, e.g. "TRADE".
            limit: Page size (API default 100, max 1000).
            cursor: Opaque next_cursor from the previous page.
            sort_direction: ASC | DESC.
            exclude_deposits_withdrawals: API default true.

        Returns:
            List of activity item dicts.
        """
        params: dict = {"limit": int(limit)}
        if user:
            params["user"] = user
        if condition:
            params["condition"] = condition
        if activity_type:
            params["type"] = activity_type
        if cursor:
            params["cursor"] = cursor
        if sort_direction:
            params["sort_direction"] = sort_direction
        if exclude_deposits_withdrawals is not None:
            params["exclude_deposits_withdrawals"] = (
                "true" if exclude_deposits_withdrawals else "false"
            )
        data, _ = self._get_v2("/v2/activity", params)
        return data

    def get_positions_v2(
        self,
        user: str,
        status: str = "OPEN",
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[dict]:
        """Positions for a user (verified v2 params).

        GET /v2/positions — one route serves the whole lifecycle via
        ``status`` (OPEN | CLOSED).

        Args:
            user: EVM address.
            status: OPEN | CLOSED.
            limit: Page size.
            cursor: Opaque next_cursor.

        Returns:
            List of position dicts (empty = valid zero-state).
        """
        params: dict = {"user": user, "status": status, "limit": int(limit)}
        if cursor:
            params["cursor"] = cursor
        data, _ = self._get_v2("/v2/positions", params)
        return data

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
