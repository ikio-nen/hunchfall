"""RFC-003 social-media analyzer — offline fixtures, zero network.

Every upstream is faked: the Jetstream websocket via
``websockets.sync.client.connect``, and the Reddit/RSS collectors via
monkeypatch. The analyzer must never raise and never block a prediction.
"""

from __future__ import annotations

import json

import app.social
from app.social import SocialAnalyzer, classify_market
from tests.fakes import make_settings


def _jetstream_event(collection: str, time_us: int, text: str | None = None) -> str:
    record = {"text": text} if text is not None else {}
    return json.dumps(
        {
            "did": "did:plc:test",
            "time_us": time_us,
            "kind": "commit",
            "commit": {
                "operation": "create",
                "collection": collection,
                "rkey": "r",
                "record": record,
            },
        }
    )


class FakeWS:
    """Minimal ``websockets.sync`` connection stand-in."""

    def __init__(self, events: list[str]) -> None:
        self.events = list(events)

    def __enter__(self) -> "FakeWS":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def recv(self, timeout: float | None = None) -> str:
        if not self.events:
            raise TimeoutError
        return self.events.pop(0)


# ------------------------------------------------------------- classifier --
def test_non_social_market_skips_the_pulse():
    verdict = classify_market(
        {"question": "Will BTC hit $100k in 2026?", "slug": "will-btc-hit-100k"}
    )
    assert verdict["relevance"] == "none"
    assert verdict["proxy"] is False


def test_social_market_is_direct_when_not_x_centric():
    verdict = classify_market(
        {"question": "Will #hunchfall trend on bluesky this week?", "slug": "trend"}
    )
    assert verdict["relevance"] == "direct"
    assert verdict["proxy"] is False


def test_x_centric_market_is_labelled_proxy():
    verdict = classify_market(
        {"question": "Will this tweet reach 1 million likes?", "slug": "tweet-likes"}
    )
    assert verdict["relevance"] == "proxy"
    assert verdict["proxy"] is True
    assert "proxy" in verdict["reason"]


# ---------------------------------------------------- tag-based routing --
def test_unrelated_category_tags_skip_the_pulse_despite_social_wording():
    """Gamma tags route the pulse: a sports market stays out of it.

    Live-checked 2026-10-02: tags arrive as ``[{"slug", "label", ...}]`` when
    the market is fetched with ``include_tag=true``.
    """
    verdict = classify_market(
        {
            "question": "Will the viral post about the final reach 1M likes?",
            "slug": "final-likes",
            "tags": [{"slug": "soccer", "label": "Soccer"}],
        }
    )
    assert verdict["relevance"] == "none"
    assert "soccer" in verdict["reason"]
    assert verdict["tags"] == ["soccer"]


def test_social_category_tags_route_the_pulse_without_social_wording():
    verdict = classify_market(
        {
            "question": "Will the launch event break the internet?",
            "slug": "launch-event",
            "tags": [{"slug": "celebrity", "label": "Celebrity"}],
        }
    )
    assert verdict["relevance"] == "direct"
    assert "social-category tags" in verdict["reason"]


def test_a_market_with_no_tags_still_routes_by_text():
    verdict = classify_market(
        {"question": "Will #hunchfall trend on bluesky?", "slug": "trend"}
    )
    assert verdict["relevance"] == "direct"
    assert verdict["tags"] == []


def test_market_tag_slugs_tolerates_strings_and_junk():
    from app.social import market_tag_slugs

    assert market_tag_slugs({"tags": [{"slug": "A"}, {"label": "B"}, "C"]}) == [
        "a",
        "b",
        "c",
    ]
    assert market_tag_slugs({"tags": "soccer"}) == []
    assert market_tag_slugs({}) == []


def test_skipped_market_is_marked_missing_not_silently_empty(tmp_path):
    """A skipped pulse must not look like a market with zero chatter."""
    settings = make_settings(tmp_path)
    block = SocialAnalyzer(settings).analyze(
        {
            "question": "Will it rain tomorrow?",
            "slug": "rain",
            "tags": [{"slug": "weather"}],
        }
    )
    assert block["relevance"] == "none"
    assert block["missing"] == ["skipped: not a social-outcome category"]
    assert "posts_window" not in block


# ------------------------------------------------------------- collectors --
def test_jetstream_window_counts_posts_and_engagement(tmp_path, monkeypatch):
    base = 1_000_000_000_000_000
    events = [
        _jetstream_event("app.bsky.feed.post", base, text="hello world"),
        _jetstream_event("app.bsky.feed.like", base + 2_000_000),
        _jetstream_event("app.bsky.feed.post", base + 4_000_000, text="second"),
    ]
    import websockets.sync.client as ws_sync

    monkeypatch.setattr(ws_sync, "connect", lambda url, **kw: FakeWS(events))
    settings = make_settings(tmp_path, SOCIAL_JETSTREAM_URL="wss://example.invalid/subscribe")
    result = app.social.collect_jetstream(settings, "terms", timeout=1.0)
    assert result is not None
    assert result["posts"] == 2
    assert result["engagements"] == 1
    # midpoint splits the three stamps into recent {t2,t3} and prior {t1}
    assert (result["recent"], result["prior"]) == (2, 1)
    assert result["top_item"]["platform"] == "bluesky"
    assert result["top_item"]["text"] == "hello world"


def test_jetstream_failure_returns_none(tmp_path, monkeypatch):
    import websockets.sync.client as ws_sync

    def _boom(url, **kw):
        raise OSError("no route to host")

    monkeypatch.setattr(ws_sync, "connect", _boom)
    settings = make_settings(tmp_path, SOCIAL_JETSTREAM_URL="wss://example.invalid/subscribe")
    assert app.social.collect_jetstream(settings, "terms", timeout=1.0) is None


# ---------------------------------------------------------------- analyzer --
def test_analyzer_marks_missing_sources_and_computes_chatter(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr(
        app.social,
        "collect_jetstream",
        lambda s, t, to: {
            "posts": 20,
            "engagements": 20,
            "recent": 3,
            "prior": 3,
            "top_item": {"platform": "bluesky", "text": "x", "url": "", "ts": ""},
        },
    )
    monkeypatch.setattr(app.social, "collect_reddit", lambda s, t, to: None)
    monkeypatch.setattr(app.social, "collect_rss", lambda s, t, to: None)

    block = SocialAnalyzer(settings).analyze(
        {"question": "Will #x trend on bluesky?", "slug": "trend"}
    )
    assert block["relevance"] == "direct"
    assert block["platforms_ok"] == ["bluesky"]
    assert set(block["missing"]) == {"reddit", "rss"}
    assert block["posts_window"] == 20
    assert block["engagement_window"] == 20
    assert block["velocity"] == 1.0  # 3 recent / 3 prior
    # 0.5*1 + 0.3*1 + 0.2*(1.0/2) = 0.9
    assert block["chatter_score"] == 0.9


def test_analyzer_chatter_defaults_velocity_when_no_prior(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr(
        app.social,
        "collect_jetstream",
        lambda s, t, to: {
            "posts": 10,
            "engagements": 0,
            "recent": 4,
            "prior": 0,
            "top_item": None,
        },
    )
    monkeypatch.setattr(app.social, "collect_reddit", lambda s, t, to: None)
    monkeypatch.setattr(app.social, "collect_rss", lambda s, t, to: None)
    block = SocialAnalyzer(settings).analyze(
        {"question": "Will #x trend on bluesky?", "slug": "trend"}
    )
    assert "velocity" not in block  # prior window empty -> omitted, not guessed
    # 0.5*(10/20) + 0.3*0 + 0.2*(1.0/2) = 0.35
    assert block["chatter_score"] == 0.35


def test_analyzer_all_sources_missing_is_honest(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr(app.social, "collect_jetstream", lambda s, t, to: None)
    monkeypatch.setattr(app.social, "collect_reddit", lambda s, t, to: None)
    monkeypatch.setattr(app.social, "collect_rss", lambda s, t, to: None)
    block = SocialAnalyzer(settings).analyze(
        {"question": "Will #x trend on bluesky?", "slug": "trend"}
    )
    assert block["platforms_ok"] == []
    assert set(block["missing"]) == {"bluesky", "reddit", "rss"}
    assert "no social source" in block["note"]
    assert "posts_window" not in block  # nothing invented


def test_analyzer_respects_an_exhausted_deadline(tmp_path, monkeypatch):
    """A zero deadline means every source is skipped, never awaited."""
    settings = make_settings(tmp_path, SOCIAL_DEADLINE_SEC=0.0)
    called: list[str] = []
    monkeypatch.setattr(
        app.social, "collect_jetstream", lambda s, t, to: called.append("js") or None
    )
    monkeypatch.setattr(
        app.social, "collect_reddit", lambda s, t, to: called.append("rd") or None
    )
    monkeypatch.setattr(
        app.social, "collect_rss", lambda s, t, to: called.append("rss") or None
    )
    block = SocialAnalyzer(settings).analyze(
        {"question": "Will #x trend on bluesky?", "slug": "trend"}
    )
    assert called == []
    assert set(block["missing"]) == {"bluesky", "reddit", "rss"}


def test_analyzer_carries_proxy_label_for_x_markets(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr(app.social, "collect_jetstream", lambda s, t, to: None)
    monkeypatch.setattr(app.social, "collect_reddit", lambda s, t, to: None)
    monkeypatch.setattr(app.social, "collect_rss", lambda s, t, to: None)
    block = SocialAnalyzer(settings).analyze(
        {"question": "Will this tweet hit 1M likes?", "slug": "tweet-likes"}
    )
    assert block["proxy"] is True
    assert "proxy" in block["reason"]


def test_analyzer_never_raises_when_a_collector_explodes(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)

    def _boom(s, t, to):
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(app.social, "collect_jetstream", _boom)
    monkeypatch.setattr(app.social, "collect_reddit", _boom)
    monkeypatch.setattr(app.social, "collect_rss", _boom)
    block = SocialAnalyzer(settings).analyze(
        {"question": "Will #x trend on bluesky?", "slug": "trend"}
    )
    assert block["platforms_ok"] == []
    assert set(block["missing"]) == {"bluesky", "reddit", "rss"}
