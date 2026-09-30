"""Story pipeline helpers: engagement filter + URL dedupe.

Engagement ranking (documented contract): ``Story.engagement_score`` is
expected to be pre-composed upstream as
``source_count x recency x GDELT volume/tone deltas`` — i.e. how many
independent sources carry the story, how fresh it is, and how sharply
GDELT's TimelineVol/TimelineTone series moved for the story's keywords
(see ``fetch_gdelt_timeline``). ``filter_by_engagement`` filters and sorts
on that composed score; it does not recompute it.
"""

from __future__ import annotations

from app.scraper import Story


def filter_by_engagement(
    stories: list[Story], min_score: float = 1.0
) -> list[Story]:
    """Keep stories at/above min_score, sorted by engagement desc.

    ``engagement_score`` is the composed source-count x recency x GDELT
    volume/tone-delta rank (see module docstring); this function only
    filters and sorts on it.

    Args:
        stories: Raw story list.
        min_score: Minimum engagement_score to keep.

    Returns:
        Filtered stories, highest engagement first.
    """
    return sorted(
        (s for s in stories if s.engagement_score >= min_score),
        key=lambda s: s.engagement_score,
        reverse=True,
    )


def dedupe_by_url(stories: list[Story]) -> list[Story]:
    """Drop duplicate stories sharing the same URL (first occurrence wins).

    Args:
        stories: Story list (order preserved).

    Returns:
        De-duplicated list.
    """
    seen: set[str] = set()
    unique: list[Story] = []
    for story in stories:
        if story.url in seen:
            continue
        seen.add(story.url)
        unique.append(story)
    return unique
