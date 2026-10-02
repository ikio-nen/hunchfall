"""Millisecond market scanner: one Gamma /markets page -> top-volume candidates.

This is the *fast* layer, complementary to the slow story scrapers and the
6-hourly Muse trend scan:

* exactly ONE HTTP GET per call (Gamma ``/markets`` page, ~100-300 ms),
* sorted by 24h volume, top-N returned,
* no AI, no files, no credentials, no human in the loop.

It runs inside the loop process, so it is fully automatic: no tokens, no
approvals, no cross-machine transport. Poll cadence is the loop's own
``LOOP_INTERVAL_SEC``. Millisecond *cadence* is deliberately not offered —
Gamma rate-limits ``/markets`` and every candidate costs a CLOB snapshot
plus a Jev call downstream; the scan itself is what runs in milliseconds.

Context discipline: a Gamma ``/markets`` page is a huge payload (100
markets × descriptions, images, event metadata...). Every market is
slimmed to ONLY the fields the pipeline actually reads (ids, question,
tokens, outcomes, prices, end date, tags, volume, resolution/UMA/fee
flags) before it leaves this module. Jev itself never sees even that —
it receives just the four ``DecisionState`` fields
(news_summary, market_question, yes_price, extra_context).

Candidates are slim Gamma market dicts. The loop resolves YES tokens,
rejects resolved markets, and runs the identical snapshot -> Jev -> gate
-> paper pipeline as every other source.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("hunchfall.fast_scan")

# Gamma volume field names seen across /markets responses; first hit wins.
_VOLUME_KEYS = (
    "volume24hr",
    "volume24Hr",
    "volume_24hr",
    "volume24H",
    "volume24h",
    "volumeUsd",
    "volume",  # last resort: may be all-time volume, better than 0.0
)


def _parse_volume(raw: Any) -> float | None:
    """Parse one volume value; None when unparseable."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return max(0.0, float(raw))
    text = str(raw).replace(",", "").replace("$", "").strip()
    try:
        return max(0.0, float(text))
    except (TypeError, ValueError):
        return None


def volume_24h(market: dict) -> float:
    """Best-effort 24h volume from a Gamma market dict (0.0 when absent)."""
    for key in _VOLUME_KEYS:
        parsed = _parse_volume(market.get(key))
        if parsed is not None:
            return parsed
    return 0.0


def has_token_pair(market: dict) -> bool:
    """True when the market carries at least two CLOB token ids."""
    raw = market.get("clobTokenIds")
    if isinstance(raw, str):
        return len([t for t in raw.split(",") if t.strip()]) >= 2
    return isinstance(raw, list) and len(raw) >= 2


# Every field the downstream pipeline reads. Anything not listed here is
# dropped at the fetch boundary — the huge Gamma payload never travels.
_SLIM_KEYS = (
    "id",
    "conditionId",
    "question",
    "clobTokenIds",
    "outcomes",
    "outcomePrices",  # negrisk_sum
    "endDate",
    "end_date",  # hours_to_resolution
    "closed",
    "resolutionSource",
    "resolvedBy",  # is_resolved
    "umaResolutionStatus",
    "uma_resolution_status",
    "umaStatus",  # uma_dispute + is_resolved
    "feeRate",
    "fee_rate",
    "takerFeeRate",
    "taker_fee_rate",  # fee override
)


def slim_market(market: dict, volume: float | None = None) -> dict:
    """Reduce a Gamma market dict to the fields the pipeline actually uses.

    Tags are normalized to plain strings (label/slug/name). Volume is
    normalized to a single ``volume24hr`` float. Everything else —
    descriptions, images, event metadata — is dropped here.

    Args:
        market: Raw Gamma market dict.
        volume: Precomputed 24h volume (recomputed when None).

    Returns:
        Minimal dict; safe for every downstream reader in app/loop.py.
    """
    slim = {
        key: market[key]
        for key in _SLIM_KEYS
        if market.get(key) is not None
    }
    tags: list[str] = []
    for tag in market.get("tags") or []:
        if isinstance(tag, dict):
            for k in ("label", "slug", "name"):
                if tag.get(k):
                    tags.append(str(tag[k]))
                    break
        elif tag:
            tags.append(str(tag))
    if tags:
        slim["tags"] = tags
    slim["volume24hr"] = volume if volume is not None else volume_24h(market)
    return slim


def fetch_top_volume_markets(
    gamma: Any,
    *,
    limit: int = 100,
    top_n: int = 5,
    min_volume_24h: float = 0.0,
    audit: Any | None = None,
    order: str | None = "volume24hr",
    ascending: bool = False,
) -> list[dict]:
    """Fetch one Gamma /markets page, return the top-N by 24h volume.

    Asks Gamma to order by 24h volume server-side so the page is the true
    global top-volume page when the server honors it. The official spec
    does not enumerate allowed ``order`` values ("volume24hr" is
    community-documented), so the client-side sort below stays as the
    correctness backstop either way.

    Args:
        gamma: GammaClient (duck-typed; only ``list_markets`` is used).
        limit: Page size for the single /markets call.
        top_n: Max candidates to return.
        min_volume_24h: Drop markets below this 24h USD volume.
        audit: Optional AuditLog for stage_error records.
        order: Server-side ordering field (None to disable).
        ascending: Server-side sort direction.

    Returns:
        Up to ``top_n`` *slimmed* market dicts (see ``slim_market``),
        sorted by 24h volume descending. Never raises: network failure
        -> [] (fail-open, other sources run).
    """
    try:
        markets, _cursor = gamma.list_markets(
            limit=limit, order=order, ascending=ascending
        )
    except Exception as exc:  # noqa: BLE001 - tolerated, recorded
        if audit is not None:
            audit.record(
                "stage_error",
                {"stage": "fast_scan.list_markets", "error": str(exc)},
            )
        return []
    scored = [
        (volume_24h(m), m)
        for m in (markets or [])
        if has_token_pair(m)
    ]
    scored = [(v, m) for v, m in scored if v >= min_volume_24h]
    scored.sort(key=lambda item: item[0], reverse=True)
    return [slim_market(m, volume=v) for v, m in scored[: max(0, top_n)]]
