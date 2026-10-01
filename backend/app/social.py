"""Social-media analyzer for social-outcome markets (RFC-003, prediction only).

Polymarket lists markets whose *resolution depends on social activity* (post
counts, engagement, account behaviour). For those markets this analyzer **is
the data source** — it answers "is the subject posting / being talked about
right now", not "is the mood good".

Hard rules (RFC-003 invariants):

* **Feature, not signal.** Nothing in this module can set a prediction's
  direction or abstention; the numbers ride along in the snapshot and the UI
  only. X-centric markets are always labelled ``proxy: true``.
* **Keyless public sources only.** Bluesky **Jetstream** websocket (the
  ``websockets`` dependency is already present), Reddit public JSON, and RSS
  from ``app/scraper/news.py`` (imported read-only). **X is excluded** — it
  has no free tier.
* **Prediction only.** This module never imports ``PaperEngine``,
  ``app.execution``, or ``app.loop`` (enforced by
  ``tests/test_scan_prediction_only.py``), never places a fill, and never
  touches a wallet.
* **Bounded and tolerant.** One shared deadline across every source; a slow
  or refusing source is *dropped*, listed in ``missing``, and never blocks
  the prediction.

Live-check notes (2026-09-30, recorded in ``docs/API_INVENTORY.md``): the
Jetstream live path is ``wss://jetstream2.us-east.bsky.network/subscribe``
(the ``/xrpc/...subscribeEvents`` suffix 404s), and keyless Reddit JSON
returned **403**, so Reddit is a best-effort source that degrades to
``missing``.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Iterable
from urllib.parse import quote_plus

import httpx

from app.config import Settings

log = logging.getLogger(__name__)

#: X is excluded by design; anything below is only used to *detect* an
#: X-centric market so the chatter can be labelled a proxy.
_X_TERMS = (
    "twitter",
    "tweet",
    "tweets",
    "retweet",
    "retweets",
    "x.com",
    "elon",
    "musk",
)

#: Keywords that mark a market as social-outcome related.
_SOCIAL_TERMS = (
    "post",
    "posts",
    "posted",
    "posting",
    "tweet",
    "tweets",
    "retweet",
    "followers",
    "follower",
    "likes",
    "views",
    "subscribers",
    "subscriber",
    "instagram",
    "tiktok",
    "youtube",
    "bluesky",
    "reddit",
    "subreddit",
    "social media",
    "social-media",
    "engagement",
    "trending",
    "viral",
    "hashtag",
    "x.com",
    "twitter",
)

_STOPWORDS = frozenset(
    {
        "will",
        "would",
        "the",
        "this",
        "that",
        "have",
        "has",
        "with",
        "from",
        "before",
        "after",
        "over",
        "under",
        "more",
        "than",
        "least",
        "most",
        "reach",
        "get",
        "gets",
        "any",
        "and",
        "for",
        "not",
        "his",
        "her",
        "they",
        "them",
        "who",
        "what",
        "when",
        "there",
        "into",
        "about",
        "post",
        "posts",
        "posted",
        "posting",
        "tweet",
        "tweets",
        "retweet",
        "followers",
        "likes",
        "views",
        "subscribers",
        "engagement",
        "viral",
        "trending",
    }
)

#: Saturation points for the 0..1 ``chatter_score`` composite.
CHATTER_POSTS_FULL = 20.0
CHATTER_ENGAGEMENTS_FULL = 20.0
CHATTER_VELOCITY_FULL = 2.0

#: Max characters kept from any single social item.
TOP_ITEM_TEXT_MAX = 200


def _clamp01(value: float) -> float:
    """Clamp a float into [0, 1]."""
    return max(0.0, min(1.0, float(value)))


def _has_term(text: str, terms: Iterable[str]) -> bool:
    """True when any term appears in ``text`` as a whole word/phrase."""
    for term in terms:
        pattern = r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])"
        if re.search(pattern, text):
            return True
    return False


def _subject_terms(market: dict) -> str:
    """Derive compact search terms from a market question/slug.

    Args:
        market: Gamma-shaped market dict.

    Returns:
        Space-joined significant words (never empty — falls back to the
        question text).
    """
    text = f"{market.get('question') or ''} {market.get('slug') or ''}"
    words = re.findall(r"[A-Za-z0-9']+", text)
    keep = [w for w in words if len(w) > 3 and w.lower() not in _STOPWORDS]
    if not keep:
        return str(market.get("question") or market.get("slug") or "").strip()
    return " ".join(keep[:8])


def classify_market(market: dict) -> dict:
    """Decide whether a market is a social-outcome market and how to read it.

    Args:
        market: Gamma-shaped market dict (uses ``question`` + ``slug``).

    Returns:
        ``{"relevance": "direct"|"proxy"|"none", "proxy": bool,
        "reason": str, "subject": str, "terms": str}``.
    """
    text = f"{market.get('question') or ''} {market.get('slug') or ''}"
    lowered = text.lower()
    terms = _subject_terms(market)
    if not _has_term(lowered, _SOCIAL_TERMS):
        return {
            "relevance": "none",
            "proxy": False,
            "reason": "not a social-outcome market — social pulse skipped",
            "subject": "",
            "terms": terms,
        }
    if _has_term(lowered, _X_TERMS):
        return {
            "relevance": "proxy",
            "proxy": True,
            "reason": (
                "X-centric market — X has no free tier, so cross-platform "
                "chatter is reported as a labelled proxy; the Bluesky window "
                "is network-wide (unfiltered)"
            ),
            "subject": terms,
            "terms": terms,
        }
    return {
        "relevance": "direct",
        "proxy": False,
        "reason": (
            "subject observable on Bluesky (keyless Jetstream window) — the "
            "window counts are network-wide (unfiltered), not subject-only"
        ),
        "subject": terms,
        "terms": terms,
    }


# --------------------------------------------------------------- collectors --
def collect_jetstream(settings: Settings, terms: str, timeout: float) -> dict | None:
    """Bounded, keyless Bluesky Jetstream window (connect -> count -> close).

    Counts ``app.bsky.feed.post`` creates in the window; likes and reposts
    whose payload is observed are treated as engagement signals.

    **The counts are network-wide and unfiltered.** The public tail is not
    keyword-filtered (no server-side or client-side subject filter in the
    MVP), so ``posts`` / ``engagements`` describe the whole Bluesky firehose
    during the window, not the market's subject. Every surface that shows
    these numbers labels them ``network-wide (unfiltered)`` rather than
    presenting them as the subject's own social pulse.

    Args:
        settings: App Settings (URL + caps).
        terms: Search/subject terms (currently used for logging only —
            the Jetstream tail is not server-side filtered in the MVP).
        timeout: Wall-clock seconds for the whole window.

    Returns:
        ``{"posts", "engagements", "recent", "prior", "top_item"}`` or None
        when the source is unavailable.
    """
    url = str(getattr(settings, "SOCIAL_JETSTREAM_URL", "") or "").strip()
    if not url:
        return None
    try:
        from websockets.sync.client import connect as ws_connect
    except Exception as exc:  # noqa: BLE001 - optional dependency, degrade
        log.warning("social: websockets unavailable: %s", exc)
        return None

    window = float(getattr(settings, "SOCIAL_JETSTREAM_WINDOW_SEC", 3.0) or 3.0)
    window = max(0.2, min(window, max(0.2, float(timeout))))
    cap = int(getattr(settings, "SOCIAL_MAX_EVENTS", 2000) or 2000)

    posts = 0
    engagements = 0
    stamps: list[float] = []
    top_item: dict | None = None
    deadline = time.monotonic() + window
    try:
        with ws_connect(url, open_timeout=min(5.0, max(1.0, window))) as ws:
            while len(stamps) < cap:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    raw = ws.recv(timeout=remaining)
                except TimeoutError:
                    break
                try:
                    event = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(event, dict):
                    continue
                commit = event.get("commit") or {}
                collection = str(commit.get("collection") or "")
                time_us = event.get("time_us")
                if isinstance(time_us, (int, float)):
                    stamps.append(float(time_us) / 1_000_000.0)
                if collection.endswith("feed.post"):
                    posts += 1
                    record = commit.get("record") or {}
                    text = record.get("text")
                    if top_item is None and isinstance(text, str) and text.strip():
                        top_item = {
                            "platform": "bluesky",
                            "text": "".join(c for c in text if c.isprintable())[
                                :TOP_ITEM_TEXT_MAX
                            ],
                            "url": "",
                            "ts": (
                                str(record.get("createdAt") or "")
                                or _iso_from_epoch(stamps[-1] if stamps else None)
                            ),
                        }
                elif collection.endswith("feed.like") or collection.endswith(
                    "feed.repost"
                ):
                    engagements += 1
    except Exception as exc:  # noqa: BLE001 - dropped source, never fatal
        log.warning("social: jetstream window failed (%s)", exc)
        return None

    recent = prior = 0
    if stamps:
        midpoint = (min(stamps) + max(stamps)) / 2.0
        recent = sum(1 for ts in stamps if ts >= midpoint)
        prior = len(stamps) - recent
    return {
        "posts": posts,
        "engagements": engagements,
        "recent": recent,
        "prior": prior,
        "top_item": top_item,
    }


def collect_reddit(settings: Settings, terms: str, timeout: float) -> dict | None:
    """Keyless Reddit search JSON (best-effort; 403s degrade to None).

    Args:
        settings: App Settings.
        terms: Search terms.
        timeout: HTTP timeout seconds.

    Returns:
        ``{"mentions", "top_item"}`` or None when unavailable.
    """
    if not terms.strip():
        return None
    url = "https://www.reddit.com/search.json"
    headers = {
        "User-Agent": str(
            getattr(settings, "REDDIT_USER_AGENT", "") or "hunchfall-paper/0.1"
        )
    }
    try:
        resp = httpx.get(
            url,
            params={"q": terms, "limit": 10, "sort": "new"},
            headers=headers,
            timeout=max(0.5, float(timeout)),
        )
        if resp.status_code >= 400:
            log.info("social: reddit returned %s (best-effort, dropped)", resp.status_code)
            return None
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 - dropped source, never fatal
        log.info("social: reddit unavailable (%s)", exc)
        return None
    children = ((payload or {}).get("data") or {}).get("children") or []
    mentions = 0
    top_item: dict | None = None
    for child in children:
        data = (child or {}).get("data") or {}
        if not isinstance(data, dict):
            continue
        mentions += 1
        if top_item is None and data.get("title"):
            top_item = {
                "platform": "reddit",
                "text": str(data.get("title"))[:TOP_ITEM_TEXT_MAX],
                "url": "https://www.reddit.com" + str(data.get("permalink") or ""),
                "ts": _iso_from_epoch(data.get("created_utc")),
            }
    return {"mentions": mentions, "top_item": top_item}


def collect_rss(settings: Settings, terms: str, timeout: float) -> dict | None:
    """RSS headlines via the existing keyless scraper (imported read-only).

    Args:
        settings: App Settings.
        terms: Search query for Google News RSS.
        timeout: Unused by feedparser; kept for a uniform collector signature.

    Returns:
        ``{"mentions", "top_item"}`` or None when unavailable.
    """
    del timeout  # feedparser has no per-call timeout; the shared deadline gates us
    query = terms.strip() or "prediction markets"
    try:
        from app.scraper.news import fetch_news_trending
    except Exception as exc:  # noqa: BLE001 - optional, degrade
        log.info("social: rss source unavailable (%s)", exc)
        return None
    try:
        stories = fetch_news_trending(settings, query=query)
    except Exception as exc:  # noqa: BLE001 - dropped source, never fatal
        log.info("social: rss fetch failed (%s)", exc)
        return None
    if not stories:
        return None
    max_items = int(getattr(settings, "SOCIAL_RSS_MAX_ITEMS", 10) or 10)
    limited = list(stories)[: max(1, max_items)]
    first = limited[0]
    return {
        "mentions": len(limited),
        "top_item": {
            "platform": "rss",
            "text": str(getattr(first, "title", ""))[:TOP_ITEM_TEXT_MAX],
            "url": str(getattr(first, "url", "") or ""),
            "ts": str(getattr(first, "published_at", "") or ""),
        },
    }


def _iso_from_epoch(value: Any) -> str:
    """Best-effort ISO-8601 UTC string from epoch seconds (or "")."""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(value)))
    except (TypeError, ValueError, OverflowError):
        return ""


class SocialAnalyzer:
    """Runs the social pulse for social-outcome markets (feature only)."""

    def __init__(self, settings: Settings) -> None:
        """Store settings.

        Args:
            settings: App Settings.
        """
        self.settings = settings

    def analyze(self, market: dict) -> dict:
        """Build the ``social`` feature block for one market.

        Never raises: an unavailable source is dropped and named in
        ``missing``; the prediction continues without it.

        Args:
            market: Gamma-shaped market dict.

        Returns:
            The ``social`` feature block (§5 of RFC-003).
        """
        verdict = classify_market(market)
        block: dict = {
            "relevance": verdict["relevance"],
            "proxy": verdict["proxy"],
            "reason": verdict["reason"],
            "platforms_ok": [],
            "missing": [],
        }
        if verdict["relevance"] == "none":
            return block
        if not bool(getattr(self.settings, "SOCIAL_ENABLED", True)):
            block["missing"] = ["disabled"]
            block["note"] = "SOCIAL_ENABLED=false — social pulse skipped"
            return block

        terms = verdict["terms"]
        # NOTE: read the setting without ``or`` — a deliberate 0.0 (used to
        # skip every source) must not be coerced back to the default.
        raw_deadline = getattr(self.settings, "SOCIAL_DEADLINE_SEC", 4.0)
        deadline_sec = 4.0 if raw_deadline is None else float(raw_deadline)
        deadline = time.monotonic() + deadline_sec

        jetstream = self._run(collect_jetstream, terms, deadline, block, "bluesky")
        reddit = None
        if bool(getattr(self.settings, "SOCIAL_REDDIT_ENABLED", True)):
            reddit = self._run(collect_reddit, terms, deadline, block, "reddit")
        else:
            block["missing"].append("reddit")
        rss = self._run(collect_rss, terms, deadline, block, "rss")

        posts = 0
        engagements = 0
        velocity: float | None = None
        if jetstream:
            posts = int(jetstream.get("posts") or 0)
            engagements = int(jetstream.get("engagements") or 0)
            prior = int(jetstream.get("prior") or 0)
            recent = int(jetstream.get("recent") or 0)
            if prior > 0:
                velocity = round(recent / prior, 4)

        chatter = round(
            _clamp01(
                0.5 * min(1.0, posts / CHATTER_POSTS_FULL)
                + 0.3 * min(1.0, engagements / CHATTER_ENGAGEMENTS_FULL)
                + 0.2 * min(1.0, (velocity or 1.0) / CHATTER_VELOCITY_FULL)
            ),
            4,
        )

        if jetstream is None and reddit is None and rss is None:
            block["note"] = "no social source answered inside the shared deadline"
            return block

        block["posts_window"] = posts
        block["engagement_window"] = engagements
        if velocity is not None:
            block["velocity"] = velocity
        block["chatter_score"] = chatter

        top_item = None
        for source in (jetstream, reddit, rss):
            if source and source.get("top_item"):
                top_item = source["top_item"]
                break
        if top_item is not None:
            block["top_item"] = top_item
        return block

    def _run(
        self,
        collector: Callable[[Settings, str, float], dict | None],
        terms: str,
        deadline: float,
        block: dict,
        name: str,
    ) -> dict | None:
        """Run one collector inside the shared deadline; record ok/missing.

        Args:
            collector: A ``collect_*`` function.
            terms: Search terms.
            deadline: Shared ``time.monotonic()`` deadline.
            block: The social block being assembled (mutated in place).
            name: Platform name for ``platforms_ok`` / ``missing``.

        Returns:
            The collector's result, or None when skipped/failed.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            block["missing"].append(name)
            return None
        try:
            result = collector(self.settings, terms, remaining)
        except Exception as exc:  # noqa: BLE001 - a source is never fatal
            log.info("social: %s dropped (%s)", name, exc)
            result = None
        if result is None:
            block["missing"].append(name)
        else:
            block["platforms_ok"].append(name)
        return result
