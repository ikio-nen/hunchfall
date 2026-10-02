"""Polymarket Data API client (read-only, no auth).

Base: https://data-api.polymarket.com (``POLYMARKET_DATA_API_URL``).

VERIFIED endpoints (Data API v2 only — the v1 routes ``/trades`` /
``/holders`` / ``/oi`` retired 2026-10-24 and no longer exist here)::

    GET /v2/prices-history  (rate limit: 200 req / 10s)
    GET /v2/activity
    GET /v2/positions
    GET /v2/trades

Used for the trade tape (feed-freshness staleness), price history, wallet
activity, positions, and per-market trade tapes — all market-data inputs to
the snapshot/gate. No trading endpoints here (paper only).

Every method speaks the verified Data API v2 contract:
``{"data", "pagination"}`` envelope, NO ``offset`` param, cursor
pagination, 429/503 + ``Retry-After`` retries, and an empty ``data`` array as a
valid zero-state.

``get_trades_v2`` (``/v2/trades?condition=``) is the RFC-003 predictor tape:
per-trade price/size/side/timestamp with **USD notional = ``size × price``**
(the rows carry no ``usdc_size``).

Raises DataApiError naming the endpoint on HTTP failure.
"""

from __future__ import annotations

import logging
import time

import httpx

from app.config import Settings
from app.polymarket.shim import ShimError, shim_fallback

log = logging.getLogger(__name__)

#: v2 error bodies carry this flag; only a ``retryable: true`` failure is
#: retried. Missing/unparseable bodies keep the old status-based behavior.
_MAX_CONDITIONS_PER_CALL = 20


class DataApiError(RuntimeError):
    """Raised when a Data API request fails; names the endpoint."""

    def __init__(
        self,
        message: str,
        *,
        trace_id: str = "",
        retryable: bool | None = None,
    ) -> None:
        """Store the upstream trace id (and retryability) with the message.

        Args:
            message: Human-readable failure, naming the endpoint.
            trace_id: Opaque upstream ``trace_id`` (empty when unavailable).
            retryable: Upstream ``retryable`` flag, or None when unknown.
        """
        super().__init__(message)
        self.trace_id = trace_id
        self.retryable = retryable


def _error_body(resp: object) -> dict:
    """Best-effort parse of a v2 error body (``{error, code, retryable, trace_id}``).

    Args:
        resp: An httpx-like response.

    Returns:
        The error dict, or ``{}`` when the body is not a JSON object.
    """
    try:
        payload = resp.json()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - a non-JSON error body is fine
        return {}
    return payload if isinstance(payload, dict) else {}


def _error_trace(resp: object, body: dict) -> tuple[str, bool | None]:
    """Extract ``(trace_id, retryable)`` from a v2 error response.

    The trace id falls back to the ``x-trace-id`` header (every v2 response
    echoes it), so the id is logged even when the body is unreadable.

    Args:
        resp: An httpx-like response.
        body: Parsed error body.

    Returns:
        ``(trace_id, retryable)``; both may be empty/None.
    """
    headers = getattr(resp, "headers", {}) or {}
    trace_id = str(body.get("trace_id") or headers.get("x-trace-id") or "")
    raw = body.get("retryable")
    retryable = bool(raw) if isinstance(raw, bool) else None
    return trace_id, retryable


def _retry_after_seconds(raw: str | None) -> float:
    """Parse a 429/503 ``Retry-After`` header; default 1s when unreadable.

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


def _unwrap_v2(payload: object) -> tuple[list, dict]:
    """Unwrap the v2 ``{data, pagination}`` envelope.

    Args:
        payload: Parsed response body.

    Returns:
        ``(data_list, pagination_dict)``; a bare list is returned as-is with
        empty pagination.
    """
    if isinstance(payload, dict):
        data = payload.get("data", [])
        pagination = payload.get("pagination") or {}
        return (data if isinstance(data, list) else []), pagination
    return (payload if isinstance(payload, list) else []), {}


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
            try:
                shim_resp = shim_fallback(
                    self.settings, "data-api", path, params, exc
                )
            except ShimError as shim_exc:
                raise DataApiError(
                    f"Data API request failed [{endpoint}]: {shim_exc}"
                ) from shim_exc
            if shim_resp is None:
                raise DataApiError(
                    f"Data API request failed [{endpoint}]: {exc}"
                ) from exc
            if shim_resp.status_code >= 400:
                raise DataApiError(
                    f"Data API request failed [{endpoint}] via shim: "
                    f"HTTP {shim_resp.status_code}"
                ) from exc
            return shim_resp.json()
        return resp.json()

    # ---- Data API v2 (verified params; v1 retires 2026-10-24) -------------
    def _get_v2(self, path: str, params: dict | None = None) -> tuple[list, dict]:
        """GET a v2 route and unwrap the ``{data, pagination}`` envelope.

        Never sends ``offset`` (v2 rejects it). A ``429``/``503`` is retried
        (up to two retries, sleep capped at 5s) when the v2 error body says
        ``retryable: true`` — or when the body is unreadable, which keeps the
        pre-existing status-based behavior. A non-retryable failure is raised
        immediately with the upstream ``trace_id`` attached, so callers can log
        it for operator correlation.

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
        last_status: int | None = None
        last_trace = ""
        for attempt in range(3):
            try:
                resp = self._http.get(path, params=clean)
                if resp.status_code in (429, 503):
                    last_status = resp.status_code
                    body = _error_body(resp)
                    trace_id, retryable = _error_trace(resp, body)
                    last_trace = trace_id or last_trace
                    if retryable is False:
                        # Upstream says the request itself is the problem;
                        # retrying it unchanged would just burn the budget.
                        message = (
                            f"Data API v2 request failed [{endpoint}]: "
                            f"HTTP {resp.status_code} {body.get('error') or ''}".strip()
                        )
                        if trace_id:
                            message = f"{message} (trace_id={trace_id})"
                        raise DataApiError(
                            message, trace_id=trace_id, retryable=False
                        )
                    if attempt >= 2:
                        break
                    retry_after = _retry_after_seconds(resp.headers.get("Retry-After"))
                    time.sleep(min(retry_after, 5.0))
                    continue
                resp.raise_for_status()
                return _unwrap_v2(resp.json())
            except DataApiError:
                raise
            except httpx.HTTPError as exc:
                try:
                    shim_resp = shim_fallback(
                        self.settings, "data-api", path, clean, exc
                    )
                except ShimError as shim_exc:
                    raise DataApiError(
                        f"Data API v2 request failed [{endpoint}]: {shim_exc}"
                    ) from shim_exc
                if shim_resp is not None:
                    if shim_resp.status_code >= 400:
                        raise DataApiError(
                            f"Data API v2 request failed [{endpoint}] via "
                            f"shim: HTTP {shim_resp.status_code}"
                        ) from exc
                    return _unwrap_v2(shim_resp.json())
                last_exc = exc
                break
        detail = (
            last_exc if last_exc is not None else f"HTTP {last_status} after retries"
        )
        message = f"Data API v2 request failed [{endpoint}]: {detail}"
        if last_trace:
            message = f"{message} (trace_id={last_trace})"
        raise DataApiError(message, trace_id=last_trace)

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

    def get_trades_v2(
        self,
        condition: str,
        limit: int = 100,
        cursor: str | None = None,
        *,
        filter_type: str | None = None,
    ) -> list[dict]:
        """Per-trade tape for one market (RFC-003 predictor).

        GET /v2/trades?condition=<condition_id>. Rows carry per-trade
        ``price``/``size``/``side``/``timestamp``; **USD notional is
        ``size × price``** — these rows have no ``usdc_size`` (see
        docs/API_INVENTORY.md). An empty list is a valid zero-state.

        ``filter_type`` is documented as ``CASH``/``TOKENS``, but
        **live-checked 2026-10-02: the condition shape ignores it** — 20/20
        CASH rows came back byte-identical to TOKENS rows, with ``size`` still
        in shares. It is accepted here (and sent when requested) purely so the
        caller can ask; USD is still computed as ``size × price`` because that
        is what the rows actually mean.

        Args:
            condition: Market condition id.
            limit: Page size.
            cursor: Opaque next_cursor from the previous page.
            filter_type: Optional CASH | TOKENS hint (accepted, not honored
                by the condition shape today).

        Returns:
            List of trade item dicts.
        """
        params: dict = {"condition": condition, "limit": int(limit)}
        if cursor:
            params["cursor"] = cursor
        if filter_type:
            params["filter_type"] = filter_type
        data, _ = self._get_v2("/v2/trades", params)
        return data

    def get_trades_v2_many(
        self, conditions: list[str], limit: int = 100
    ) -> dict[str, list[dict]]:
        """One ``/v2/trades`` call for up to 20 markets, split client-side.

        The docs allow at most 20 distinct comma-separated ``condition`` ids
        per call, and the feed interleaves them (live-checked 2026-10-02: a
        two-id batch returned rows for both). Rows are grouped by their own
        ``condition_id`` — **a row whose id was not requested is dropped**, so a
        silently-ignored filter can never be mistaken for a match.

        More than 20 ids are chunked into consecutive batches; ``limit``
        applies per call.

        Args:
            conditions: Condition ids (duplicates collapse, order kept).
            limit: Page size per call.

        Returns:
            ``{condition_id: [rows]}`` — every requested id is a key, with an
            empty list when that market had no rows (the meaningful
            zero-state).
        """
        wanted = [str(c) for c in dict.fromkeys(conditions) if str(c)]
        out: dict[str, list[dict]] = {c: [] for c in wanted}
        if not wanted:
            return out
        for start in range(0, len(wanted), _MAX_CONDITIONS_PER_CALL):
            chunk = wanted[start : start + _MAX_CONDITIONS_PER_CALL]
            rows = self.get_trades_v2(condition=",".join(chunk), limit=limit)
            allowed = set(chunk)
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                key = str(row.get("condition_id") or "")
                if key in allowed:
                    out[key].append(row)
                else:
                    log.warning(
                        "data-api: /v2/trades returned unrequested condition %r",
                        key,
                    )
        return out

    def get_prices_history_v2(
        self,
        token_id: str,
        start_ts: int | None = None,
        end_ts: int | None = None,
        interval: str = "1h",
    ) -> list[dict]:
        """Price history (v2) for a token.

        GET /v2/prices-history — 200/10s. The v2 route is keyed by
        ``token_id`` (unlike CLOB's ``/prices-history``, which takes
        ``market``); both are sent with their own documented key.

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

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._http.close()
