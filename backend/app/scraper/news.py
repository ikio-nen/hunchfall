"""RSS news source: Google News RSS + outlet RSS feeds (no API key).

Why RSS: NewsAPI.org and similar aggregators are excluded — too expensive
for a polling loop at hackathon scale. Google News RSS
(``https://news.google.com/rss/search``) plus direct outlet feeds
(Reuters/AP/BBC/Guardian) are free and keyless, parsed with ``feedparser``.

Outlet feed URLs are best-effort (publishers move them); a dead feed is
skipped with a logged warning — the pipeline tolerates failures and the
loop records a ``stage_error``. Nothing here invents stories: an empty
result is honestly [].
"""

from __future__ import annotations

import logging
import time
from urllib.parse import quote_plus

import feedparser

from app.config import Settings
from app.scraper import Story

log = logging.getLogger(__name__)

# Google News RSS search — keyless, returns up to ~100 items.
_GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"

# Outlet world-news feeds (best-effort URLs; failures are tolerated).
_OUTLET_FEEDS = {
    "reuters": "https://www.reuters.com/rssfeed/worldNews",
    "ap": "https://feedx.net/rss/ap.xml",
    "bbc": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "guardian": "https://www.theguardian.com/world/rss",
}


def _entry_published(entry) -> str:
    """Best-effort ISO-8601 published timestamp from a feedparser entry."""
    for key in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, key, None)
        if parsed:
            try:
                return time.strftime("%Y-%m-%dT%H:%M:%SZ", parsed)
            except (TypeError, ValueError):
                continue
    return str(getattr(entry, "published", "") or "")


def _stories_from_feed(url: str, source: str, limit: int) -> list[Story]:
    """Parse one RSS feed URL into Story objects.

    Args:
        url: RSS feed URL.
        source: Source label for the stories.
        limit: Max stories from this feed.

    Returns:
        List of Story objects (recency-ranked engagement heuristic).
    """
    stories: list[Story] = []
    try:
        feed = feedparser.parse(url)
    except Exception as exc:  # noqa: BLE001 - tolerated, logged
        log.warning("news: feed %s parse failed: %s", url, exc)
        return []
    if getattr(feed, "bozo", False) and not getattr(feed, "entries", None):
        log.warning("news: feed %s unreadable (bozo)", url)
        return []
    for i, entry in enumerate(feed.entries[:limit]):
        title = str(getattr(entry, "title", "") or "").strip()
        link = str(getattr(entry, "link", "") or "").strip()
        if not title:
            continue
        # Recency proxy: earlier in a fresh feed ~= hotter; decays with rank.
        stories.append(
            Story(
                title=title,
                url=link,
                source=source,
                engagement_score=max(1.0, 10.0 - i),
                published_at=_entry_published(entry),
                raw_text=str(getattr(entry, "summary", "") or ""),
            )
        )
    return stories


def fetch_news_trending(
    settings: Settings, query: str = "prediction markets"
) -> list[Story]:
    """Fetch trending news via Google News RSS + outlet RSS feeds.

    No API key needed. NewsAPI.org is deliberately NOT used (too
    expensive); everything here is free RSS parsed with feedparser.

    Args:
        settings: App Settings (unused; kept for pipeline signature).
        query: Search query for Google News RSS.

    Returns:
        List of Story objects; [] with a warning when all feeds fail.
    """
    del settings  # keyless sources — no credentials required
    stories: list[Story] = []
    gnews_url = (
        f"{_GOOGLE_NEWS_RSS}?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    )
    stories.extend(_stories_from_feed(gnews_url, "google-news", 25))
    for outlet, url in _OUTLET_FEEDS.items():
        stories.extend(_stories_from_feed(url, f"rss-{outlet}", 10))
    if not stories:
        log.warning("news: all RSS feeds returned nothing — skipping")
    return stories
