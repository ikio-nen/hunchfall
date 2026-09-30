"""Story intake package.

Sources: reddit.py (OAuth app-only; needs Reddit pre-approval since Nov
2025 — optional), news.py (Google News RSS + outlet RSS via feedparser;
NewsAPI.org deliberately excluded as too expensive), gdelt.py (public
GDELT 2.1 DOC API: ArtList + TimelineVol/TimelineTone), twitter.py
(EXCLUDED BY DESIGN — no fetch, X is pay-per-use + ToS risk).

Pipeline helpers: filter.py (engagement filter + URL dedupe),
summarizer.py (extractive stub, swappable via the Summarizer protocol).
"""

from dataclasses import dataclass


@dataclass
class Story:
    """One trending story fed to story->market matching.

    Fields:
        title: Story headline.
        url: Canonical link (used for dedupe).
        source: Source name, e.g. "reddit", "newsapi", "gdelt".
        engagement_score: Heuristic engagement rank (higher = hotter).
        published_at: ISO-8601 timestamp string (may be "" if unknown).
        raw_text: Full text / snippet for summarization.
    """

    title: str
    url: str
    source: str
    engagement_score: float
    published_at: str
    raw_text: str


__all__ = ["Story"]
