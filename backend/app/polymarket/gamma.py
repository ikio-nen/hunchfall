"""Gamma Data API client (read-only, no auth).

VERIFIED endpoints (base: https://gamma-api.polymarket.com)::

    GET /events               (rate limit: 500 req / 10s)
    GET /events/{id}
    GET /events/slug/{slug}
    GET /markets              (rate limit: 300 req / 10s)
    GET /markets/{id}
    GET /markets/slug/{slug}
    GET /public-search        (rate limit: 350 req / 10s)
    GET /tags
    GET /series
    GET /sports

General rate limit: 4000 req / 10s. Pagination on list endpoints is
keyset-based: response carries ``next_cursor`` -> pass as ``after_cursor``
for the next page (no plain ``offset`` paging — old ``offset`` params are
dropped).

Market lookups: ``/markets/{id}`` takes the **numeric** Gamma market id
(e.g. ``559651``). A raw **condition id is rejected** there — live-checked
2026-10-01, ``GET /markets/0x<64hex>`` returns
``{"type": "validation error", "error": "id is invalid"}``. Resolve a
condition id with the verified ``GET /markets?condition_ids=<id>`` filter
(``get_market_by_condition_id``).

Market fields of interest: ``question``, ``description``, ``outcomes``,
``outcomePrices`` (**JSON STRING — must be parsed**), ``clobTokenIds``
(**also a JSON STRING array**, e.g. ``'["3233822…", "2565931…"]'`` —
live-checked 2026-10-01; splitting it on commas yields ids wrapped in
``["`` … ``"]``, which CLOB rejects), ``volume`` (cumulative USD),
``liquidity``, ``endDate``, ``closed``, ``resolutionSource``.

GOTCHA: resolved markets may report ``outcomePrices`` as ``["0","0"]`` —
a resolved-to-NO market reads as all zeros, which is indistinguishable
from a broken feed. ``parse_outcome_prices()`` returns the parsed prices
and ``is_resolved()`` checks winner fields; loop.py must validate a
winner (e.g. non-empty ``resolutionSource`` / ``umaResolutionStatus``)
before trusting zeroed prices. Never trade on ``["0","0"]`` without a
confirmed winner.

Raises GammaError naming the endpoint on HTTP failure.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import httpx

from app.config import Settings

log = logging.getLogger(__name__)


class GammaError(RuntimeError):
    """Raised when a Gamma Data API request fails; names the endpoint."""


def parse_outcome_prices(market: dict) -> list[float]:
    """Parse Gamma ``outcomePrices`` into a list of floats.

    The field arrives as a JSON *string* (e.g. ``'["0.62","0.38"]'``) and
    must be parsed. Returns [] when unparseable.

    Args:
        market: Gamma market dict.

    Returns:
        Parsed price floats, position-aligned with ``outcomes``.
    """
    raw = market.get("outcomePrices")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("gamma: unparseable outcomePrices: %r", raw)
            return []
        raw = parsed
    if not isinstance(raw, (list, tuple)):
        return []
    prices: list[float] = []
    for p in raw:
        try:
            prices.append(float(p))
        except (TypeError, ValueError):
            prices.append(0.0)
    return prices


def parse_token_ids(market: dict) -> list[str]:
    """Parse Gamma ``clobTokenIds`` (JSON-array string, or a list).

    Live-checked 2026-10-01: the field arrives as a **JSON-encoded array
    string** — ``'["3233822…", "2565931…"]'`` — exactly like
    ``outcomePrices``, *not* as a bare comma-separated list. Splitting it on
    "," used to yield ``'["3233822…"'`` (brackets and quotes included); CLOB
    then answered ``{"error": …}`` with no bids, so every book-dependent
    path (``POST /predict``, the extension scan, the loop) failed against the
    real API. A bare comma-separated string is still accepted for robustness.

    Args:
        market: Gamma market dict.

    Returns:
        List of token id strings — empty when the field is missing or
        unparseable, which callers treat as "not tradeable" rather than
        guessing an id.
    """
    raw = market.get("clobTokenIds")
    if isinstance(raw, (list, tuple)):
        return [str(t) for t in raw]
    if not isinstance(raw, str):
        return []
    text = raw.strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            log.warning("gamma: unparseable clobTokenIds: %r", raw)
            return []
        if not isinstance(parsed, list):
            return []
        return [str(t).strip() for t in parsed if str(t).strip()]
    return [t.strip() for t in text.split(",") if t.strip()]


def is_resolved(market: dict) -> bool:
    """Best-effort resolved check: market closed AND a winner/resolution
    field is present.

    Resolved markets may report ``outcomePrices == ["0","0"]`` — all zeros
    with no winner field means the feed is broken (or mid-resolution), NOT
    a confirmed NO. Callers must confirm a winner before trusting zeroed
    prices.

    Args:
        market: Gamma market dict.

    Returns:
        True when the market looks resolved with a recorded winner.
    """
    closed = bool(market.get("closed"))
    winner = (
        market.get("umaResolutionStatus")
        or market.get("resolutionSource")
        or market.get("resolvedBy")
        or ""
    )
    return closed and bool(str(winner).strip())


def negrisk_sum(market: dict) -> float | None:
    """Sum of parsed outcomePrices (Yes+No token prices should total ~1).

    Args:
        market: Gamma market dict.

    Returns:
        Price sum, or None when prices are unparseable.
    """
    prices = parse_outcome_prices(market)
    if not prices:
        return None
    return sum(prices)


class GammaClient:
    """Sync read-only client for the Polymarket Gamma Data API."""

    def __init__(self, settings: Settings) -> None:
        """Store settings and create a sync httpx client (no auth).

        Args:
            settings: App Settings carrying POLYMARKET_GAMMA_URL.
        """
        self.settings = settings
        self._http = httpx.Client(base_url=settings.POLYMARKET_GAMMA_URL, timeout=20.0)

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        """GET helper that raises GammaError naming the endpoint on failure.

        Args:
            path: Path under the Gamma base URL, e.g. "/events".
            params: Query params.

        Raises:
            GammaError: on any HTTP error, naming the endpoint.
        """
        endpoint = f"{self.settings.POLYMARKET_GAMMA_URL}{path}"
        try:
            resp = self._http.get(path, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise GammaError(f"Gamma API request failed [{endpoint}]: {exc}") from exc
        return resp.json()

    # ---- verified read endpoints -----------------------------------------
    def search_events(self, query: str, limit: int = 20) -> list[dict]:
        """Search events matching a story keyword.

        GET /events — rate limit 500/10s.

        Args:
            query: Free-text story keyword(s), e.g. "election".
            limit: Max events to return.

        Returns:
            List of event dicts.
        """
        payload = self._get("/events", params={"q": query, "limit": limit})
        return payload if isinstance(payload, list) else payload.get("data", [])

    def get_event(self, event_id: str) -> dict:
        """Fetch one event (with nested markets) by id.

        GET /events/{id} — rate limit 500/10s.

        Args:
            event_id: Gamma event id.

        Returns:
            Event dict.
        """
        return self._get(f"/events/{event_id}")

    def get_event_by_slug(self, slug: str) -> dict:
        """Fetch one event by slug. GET /events/slug/{slug}.

        Args:
            slug: Event slug.

        Returns:
            Event dict.
        """
        return self._get(f"/events/slug/{slug}")

    def get_market(self, market_id: str, include_tags: bool = True) -> dict:
        """Fetch one market by its *numeric* Gamma market id.

        GET /markets/{id} — rate limit 300/10s. ``include_tag=true`` attaches
        the ``tags`` array (see ``get_market_by_slug``).

        A condition id is **not** a valid ``{id}``: live-checked 2026-10-01,
        ``/markets/0x<64hex>`` answers ``{"type": "validation error",
        "error": "id is invalid"}``. Use ``get_market_by_condition_id`` for
        that (see ``resolve_market`` in ``app/predict.py``).

        Args:
            market_id: Numeric Gamma market id, e.g. "559651".
            include_tags: Ask Gamma for the tags array (default True).

        Returns:
            Market dict.
        """
        params = {"include_tag": "true"} if include_tags else None
        return self._get(f"/markets/{market_id}", params=params)

    def get_market_by_condition_id(self, condition_id: str) -> dict:
        """Resolve a raw condition id to its market (the verified path).

        GET /markets?condition_ids=<id> is the only working condition-id
        lookup — the path form ``/markets/{condition_id}`` is rejected as an
        invalid id. Live-checked 2026-10-01, and the filter has two traps:

        * the id matches **case-sensitively**, so it is lowercased first;
        * by default Gamma returns **open** markets only, so a *resolved*
          market matches nothing until the query is repeated with
          ``closed=true``. Without that retry a resolved market is
          indistinguishable from an unknown id, and callers report
          "not found" instead of "closed/resolved".

        A returned row is accepted only when its ``conditionId`` really
        matches the requested id, so a silently-ignored filter (Gamma ignores
        unknown params and serves a default page — see ``market_ids``) can
        never masquerade as a hit.

        Args:
            condition_id: 0x-prefixed condition id.

        Returns:
            The matching market dict, or ``{}`` when nothing matches (callers
            map that to their own not-found error).
        """
        needle = str(condition_id).strip().lower()
        if not needle:
            return {}
        for extra in ({}, {"closed": "true"}):
            params = {"condition_ids": needle, "limit": 1}
            params.update(extra)
            payload = self._get("/markets", params=params)
            if isinstance(payload, list):
                rows = payload
            elif isinstance(payload, dict):
                rows = payload.get("data") or []
            else:
                rows = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                found = str(row.get("conditionId") or "").strip().lower()
                if found and found == needle:
                    return row
        return {}

    def get_market_by_slug(self, slug: str, include_tags: bool = True) -> dict:
        """Fetch one market by slug. GET /markets/slug/{slug}.

        ``include_tag=true`` attaches the market's ``tags`` array (live-checked
        2026-10-02: ``[{"id", "label", "slug", ...}]``), which the social
        analyzer uses to route only genuinely social-outcome markets to the
        full social pulse. Tags are additive; a response without them still
        parses.

        Args:
            slug: Market slug.
            include_tags: Ask Gamma for the tags array (default True).

        Returns:
            Market dict.
        """
        params = {"include_tag": "true"} if include_tags else None
        return self._get(f"/markets/slug/{slug}", params=params)

    def list_markets(
        self, limit: int = 100, after_cursor: str | None = None
    ) -> tuple[list[dict], str | None]:
        """Fetch one page of markets (keyset pagination).

        GET /markets — rate limit 300/10s. The response carries
        ``next_cursor``; pass it back as ``after_cursor`` for the next page.

        Args:
            limit: Page size.
            after_cursor: Keyset cursor from the previous page, or None.

        Returns:
            (markets, next_cursor) — next_cursor is None at the last page.
        """
        params: dict = {"limit": limit}
        if after_cursor:
            params["after_cursor"] = after_cursor
        payload = self._get("/markets", params=params)
        if isinstance(payload, list):
            return payload, None
        return payload.get("data", []), payload.get("next_cursor")

    def iter_markets(self, page_size: int = 100) -> Iterator[dict]:
        """Yield every market across keyset pages until cursor is exhausted.

        Args:
            page_size: Markets per page.

        Yields:
            Market dicts.
        """
        cursor: str | None = None
        while True:
            page, cursor = self.list_markets(limit=page_size, after_cursor=cursor)
            if not page:
                return
            yield from page
            if cursor is None:
                return

    def search_markets_public(self, query: str, limit: int = 20) -> list[dict]:
        """Search markets by free text (story -> market matching).

        GET /public-search — rate limit 350/10s. Primary story->market
        matcher for the loop.

        Args:
            query: Free-text story keyword(s).
            limit: Max markets to return.

        Returns:
            List of market dicts.
        """
        payload = self._get("/public-search", params={"q": query, "limit": limit})
        if isinstance(payload, list):
            return payload
        # some responses nest under a key
        for key in ("data", "markets", "events"):
            if isinstance(payload, dict) and isinstance(payload.get(key), list):
                return payload[key]
        return []

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._http.close()
