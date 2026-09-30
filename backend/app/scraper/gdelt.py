"""GDELT 2.1 DOC API trending-story source.

Public, no API key required. VERIFIED URL pattern::

    https://api.gdeltproject.org/api/v2/doc/doc?query=<q>&mode=ArtList
        &format=json&maxrecords=250&timespan=3d

Modes used here:
- ``ArtList`` — article list per query (story candidates),
- ``TimelineVol`` — volume timeline (for engagement / momentum deltas),
- ``TimelineTone`` — tone timeline (for sentiment deltas).

The engagement filter (``app/scraper/filter.py``) is documented to rank
by source-count x recency x GDELT volume/tone deltas; ``fetch_gdelt_timeline``
supplies the volume/tone series this needs.

Gate: settings.GDELT_ENABLED must be True (default), else returns [].
"""

from __future__ import annotations

import logging

import httpx

from app.config import Settings
from app.scraper import Story

log = logging.getLogger(__name__)

_GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"


def _doc_get(params: dict) -> dict | list:
    """GET the GDELT DOC API; returns {} on failure (tolerated, logged)."""
    try:
        with httpx.Client(timeout=20.0) as http:
            resp = http.get(_GDELT_ENDPOINT, params=params)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        log.warning("gdelt: fetch failed: %s", exc)
        return {}


def fetch_gdelt_trending(
    settings: Settings,
    query: str = "election OR economy",
    timespan: str = "3d",
) -> list[Story]:
    """Fetch trending articles from GDELT for a query + timespan.

    VERIFIED: mode=ArtList, format=json, maxrecords=250, timespan=3d —
    free, keyless.

    Args:
        settings: App Settings (GDELT_ENABLED gate).
        query: GDELT keyword query.
        timespan: e.g. "3d".

    Returns:
        List of Story objects; [] with a warning if disabled/failing.
    """
    if not settings.GDELT_ENABLED:
        log.warning("gdelt: GDELT_ENABLED=false — skipping")
        return []

    payload = _doc_get(
        {
            "query": query,
            "mode": "ArtList",
            "format": "json",
            "timespan": timespan,
            "maxrecords": 250,
            "sortby": "tone",
        }
    )
    stories: list[Story] = []
    articles = payload.get("articles", []) if isinstance(payload, dict) else []
    for art in articles:
        stories.append(
            Story(
                title=art.get("title", ""),
                url=art.get("url", ""),
                source="gdelt",
                engagement_score=1.0,  # refined by filter via timeline deltas
                published_at=str(art.get("seendate", "")),
                raw_text=art.get("title", ""),
            )
        )
    return stories


def fetch_gdelt_timeline(
    query: str,
    mode: str = "TimelineVol",
    timespan: str = "3d",
) -> list[dict]:
    """Fetch a GDELT volume/tone timeline for a query.

    VERIFIED: mode=TimelineVol | TimelineTone, format=json — free,
    keyless. Used for engagement ranking: volume/tone *deltas* (recent
    vs baseline) feed the filter's source-count x recency x delta score.

    Args:
        query: GDELT keyword query.
        mode: "TimelineVol" (article volume) or "TimelineTone" (avg tone).
        timespan: e.g. "3d".

    Returns:
        List of timeline points, each ``{"date": ..., "value": float}``
        (volume count or tone); [] on failure.
    """
    if mode not in ("TimelineVol", "TimelineTone"):
        raise ValueError(f"gdelt: unknown timeline mode {mode!r}")
    payload = _doc_get(
        {
            "query": query,
            "mode": mode,
            "format": "json",
            "timespan": timespan,
        }
    )
    if not isinstance(payload, dict):
        return []
    timeline = payload.get("timeline", [{}])[0] if payload.get("timeline") else {}
    points = timeline.get("data", []) if isinstance(timeline, dict) else []
    out: list[dict] = []
    for pt in points:
        try:
            out.append(
                {
                    "date": pt.get("date", ""),
                    "value": float(pt.get("value", pt.get("volume", 0.0)) or 0.0),
                }
            )
        except (TypeError, ValueError):
            continue
    return out
