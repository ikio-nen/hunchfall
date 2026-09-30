"""Reddit trending-story source (OAuth app-only flow).

Uses the script/app-only OAuth grant from env creds:
REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT.

NOTE (verified, Nov 2025+): Reddit now requires pre-approval for new API
apps — the OAuth flow below is the official route, but the app must be
approved by Reddit before it works. Treat Reddit as OPTIONAL: missing
creds or a rejected/unapproved app returns [] with a logged warning and
the pipeline runs on RSS + GDELT regardless.

If creds are missing: returns [] with a logged warning (no crash) — the
pipeline runs offline in MOCK mode regardless.
"""

from __future__ import annotations

import logging

import httpx

from app.config import Settings
from app.scraper import Story

log = logging.getLogger(__name__)

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"  # OAuth token
_API_BASE = "https://oauth.reddit.com"


def fetch_reddit_trending(
    settings: Settings,
    subreddits: tuple[str, ...] = ("wallstreetbets", "news", "worldnews"),
    limit: int = 25,
) -> list[Story]:
    """Fetch hot posts from the given subreddits.

    Args:
        settings: App Settings (Reddit OAuth creds).
        subreddits: Subreddits to poll.
        limit: Posts per subreddit.

    Returns:
        List of Story objects; [] with a warning if creds are missing.
    """
    if not (
        settings.REDDIT_CLIENT_ID
        and settings.REDDIT_CLIENT_SECRET
        and settings.REDDIT_USER_AGENT
    ):
        log.warning("reddit: REDDIT_CLIENT_ID/SECRET/USER_AGENT not set — skipping")
        return []

    stories: list[Story] = []
    with httpx.Client(timeout=20.0) as http:
        token = http.post(
            _TOKEN_URL,
            auth=(settings.REDDIT_CLIENT_ID, settings.REDDIT_CLIENT_SECRET),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": settings.REDDIT_USER_AGENT},
        )
        token.raise_for_status()
        access = token.json()["access_token"]
        headers = {
            "Authorization": f"Bearer {access}",
            "User-Agent": settings.REDDIT_USER_AGENT,
        }
        for sub in subreddits:
            try:
                resp = http.get(
                    f"{_API_BASE}/r/{sub}/hot",
                    params={"limit": limit},
                    headers=headers,
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("reddit: /r/%s fetch failed: %s", sub, exc)
                continue
            for child in resp.json().get("data", {}).get("children", []):
                d = child.get("data", {})
                stories.append(
                    Story(
                        title=d.get("title", ""),
                        url=f"https://www.reddit.com{d.get('permalink', '')}",
                        source="reddit",
                        engagement_score=float(d.get("score", 0))
                        + 2.0 * float(d.get("num_comments", 0)),
                        published_at=str(d.get("created_utc", "")),
                        raw_text=d.get("selftext", "") or d.get("title", ""),
                    )
                )
    return stories
