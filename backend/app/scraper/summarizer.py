"""Extractive summarizer STUB for stories.

``summarize`` is a simple extractive heuristic (first sentence + highest
keyword-overlap sentences) — deterministic, offline, dependency-free.

This is swappable later: any class implementing the ``Summarizer``
protocol (method ``summarize(story: Story) -> str``) can replace it, e.g.
an LLM-backed summarizer. Wire the replacement in the pipeline, not here.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Protocol

from app.scraper import Story

_STOPWORDS = frozenset(
    "a an the and or but of in on to for with is are was were be been has have had "
    "it its this that these those as at by from we you he she they them his her "
    "their our your not no yes will would can could should may might do does did "
    "so if than then there here when where which who whom what how why".split()
)


class Summarizer(Protocol):
    """Protocol for pluggable summarizers (LLM or otherwise)."""

    def summarize(self, story: Story) -> str:
        """Return a short summary of the story.

        Args:
            story: The story to summarize.

        Returns:
            Summary text.
        """
        ...


def _sentences(text: str) -> list[str]:
    """Split text into sentences on punctuation boundaries."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def summarize(story: Story, max_sentences: int = 3) -> str:
    """Extract key sentences: first sentence + top keyword-overlap sentences.

    Heuristic: the first sentence anchors the topic; remaining sentences
    are ranked by keyword-overlap with the story's most frequent
    non-stopword tokens (title + text). STUB — deterministic and offline.

    Args:
        story: Story to summarize.
        max_sentences: Max sentences in the summary.

    Returns:
        Summary string ("" if the story has no text).
    """
    text = story.raw_text.strip() or story.title.strip()
    sentences = _sentences(text)
    if not sentences:
        return ""
    if len(sentences) <= max_sentences:
        return " ".join(sentences)

    tokens = re.findall(r"[a-z0-9]+", text.lower())
    keywords = Counter(t for t in tokens if t not in _STOPWORDS)
    top_terms = {w for w, _ in keywords.most_common(20)}

    def overlap(s: str) -> int:
        words = set(re.findall(r"[a-z0-9]+", s.lower()))
        return len(words & top_terms)

    ranked = sorted(sentences[1:], key=overlap, reverse=True)
    chosen = [sentences[0]] + ranked[: max_sentences - 1]
    return " ".join(chosen)
