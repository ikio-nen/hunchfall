"""Trading-loop orchestrator for hunchfall (paper mode).

Pipeline per cycle::

    stories (reddit + rss news + gdelt; X excluded by design) -> dedupe
      -> engagement filter -> summarize -> gamma (story -> event -> market)
      -> clob (mid / spread / depth / book levels) + data-api (trade tape)
      -> Jev P(true) + choice + confidence (one typed call, 3 questions)
      -> RiskGate (quarter-Kelly, fee-adjusted edge, vetoes)
      -> PaperEngine (paper fill, category taker fee, book-walk)
      -> audit log + data/state.json

PAPER TRADING ONLY. Every stage failure is caught, recorded as a
``stage_error`` audit event, and tolerated: with no sources configured the
loop honestly reports ``"no opportunities"`` instead of inventing trades.

Usage (from backend/)::

    python -m app.loop --once    # single cycle, prints JSON summary
    python -m app.loop --watch   # loop every LOOP_INTERVAL_SEC
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.execution.paper import Position, PaperEngine, mark_to_market
from app.memory.audit import AuditLog
from app.paths import (
    data_dir,
    read_killswitch,
    resolve_db_path,
    state_path,
    utcnow_iso,
    write_killswitch,
)
from app.policy.gate import RiskGate, Signal
from app.polymarket.clob import ClobClient, book_levels
from app.polymarket.data_api import DataApiClient
from app.polymarket.gamma import GammaClient, negrisk_sum, parse_token_ids
from app.scraper import Story
from app.scraper.filter import dedupe_by_url, filter_by_engagement
from app.scraper.gdelt import fetch_gdelt_trending
from app.scraper.news import fetch_news_trending
from app.scraper.reddit import fetch_reddit_trending
from app.scraper.summarizer import summarize
from app.jev.client import DecisionState, JevClient

MAX_STORIES_PER_CYCLE = 5
STATE_LIST_KEEP = 50
BOOK_DEPTH_LEVELS = 5  # top-N bid/ask levels kept for the book-walk + depth

# Fee-category keyword map: scanned against market tags/question/event
# title. First hit wins; unknown markets fall back to "Other".
_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Crypto": ("crypto", "bitcoin", "btc", "ethereum", "eth", "solana"),
    "Sports": ("sports", "nfl", "nba", "mlb", "soccer", "football", "tennis"),
    "Politics": ("politics", "election", "president", "congress", "senate"),
    "Finance": ("finance", "fed", "interest rate", "stocks", "earnings"),
    "Tech": ("tech", "ai", "apple", "nvidia", "openai"),
    "Geopolitics": ("geopolitics", "war", "ukraine", "gaza", "taiwan"),
    "Economics": ("economics", "gdp", "inflation", "cpi", "recession"),
    "Culture": ("culture", "celebrity", "music", "movie", "oscars"),
    "Weather": ("weather", "hurricane", "temperature"),
    "Mentions": ("mentions",),
}


# --------------------------------------------------------------------------- #
# market picking + snapshot helpers                                           #
# --------------------------------------------------------------------------- #
def _as_list(value: Any) -> list:
    """Parse a value that may be a JSON string or an actual list."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _market_tokens(market: dict) -> tuple[str, str] | None:
    """Return (yes_token_id, no_token_id) for a Gamma market dict, or None.

    Uses the shared ``parse_token_ids`` so the JSON-array-string form Gamma
    actually returns (``'["a", "b"]'``, live-checked 2026-10-01) is parsed
    identically here and in the predictor/scan paths.
    """
    token_ids = parse_token_ids(market)
    outcomes = _as_list(market.get("outcomes"))
    if len(token_ids) < 2:
        return None
    try:
        yes_idx = next(
            i for i, o in enumerate(outcomes) if str(o).lower() == "yes"
        )
    except StopIteration:
        yes_idx = 0
    no_idx = 1 if yes_idx == 0 else 0
    if yes_idx >= len(token_ids) or no_idx >= len(token_ids):
        return None
    return str(token_ids[yes_idx]), str(token_ids[no_idx])


def _hours_to_resolution(market: dict) -> float:
    """Parse Gamma ``endDate`` into hours from now; 0.0 when unknown.

    Unknown resolution time is treated as *imminent* (fail-closed: the gate
    vetoes ``near_resolution``) rather than assumed far away.
    """
    raw = market.get("endDate") or market.get("end_date") or ""
    if not raw:
        return 0.0
    try:
        end = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0.0, (end - datetime.now(timezone.utc)).total_seconds() / 3600.0)


def _fee_category(market: dict, event_title: str = "") -> str:
    """Map a market to a taker-fee category from tags/question/title.

    Scans Gamma ``tags`` (label/slug/name) plus the question and event
    title for category keywords; falls back to "Other".

    Args:
        market: Gamma market dict.
        event_title: Parent event title (extra keyword surface).

    Returns:
        One of the FEE_RATES keys in app/execution/paper.py.
    """
    haystacks: list[str] = []
    for tag in market.get("tags") or []:
        if isinstance(tag, dict):
            haystacks.extend(
                str(tag.get(k, "")) for k in ("label", "slug", "name")
            )
        else:
            haystacks.append(str(tag))
    haystacks.append(str(market.get("question", "")))
    haystacks.append(event_title)
    blob = " ".join(haystacks).lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in blob for kw in keywords):
            return category
    return "Other"


def _fee_rate_override(market: dict, settings: Settings) -> float | None:
    """Per-market authoritative fee rate, when the payload carries one.

    TODO: exact per-market fee payload field names are UNVERIFIED against
    a live Gamma /markets response — the candidates below are guesses.
    Verify live and keep only the real one(s). Falls back to
    FEE_RATE_OVERRIDE from env (0 = disabled -> category table).

    Args:
        market: Gamma market dict.
        settings: App Settings.

    Returns:
        Fee rate fraction, or None to use the category table.
    """
    for key in ("feeRate", "fee_rate", "takerFeeRate", "taker_fee_rate"):
        raw = market.get(key)
        if raw is None:
            continue
        try:
            rate = float(raw)
        except (TypeError, ValueError):
            continue
        if 0.0 <= rate <= 1.0:
            return rate
    env_override = float(settings.FEE_RATE_OVERRIDE or 0.0)
    return env_override if env_override > 0 else None


def _uma_dispute(market: dict) -> bool:
    """Best-effort UMA-dispute flag from the market payload.

    TODO: the exact Gamma field name for UMA resolution status is
    UNVERIFIED — verify against a live /markets response. Candidates are
    checked defensively below.
    """
    for key in ("umaResolutionStatus", "uma_resolution_status", "umaStatus"):
        raw = market.get(key)
        if raw is None:
            continue
        if str(raw).strip().lower() in ("disputed", "dispute", "true", "1"):
            return True
    return False


def _pick_market(gamma: GammaClient, story: Story, audit: AuditLog) -> dict | None:
    """Match a story to one Polymarket market via Gamma search.

    Tries /public-search (story->market) first, then event search as a
    fallback. Returns ``{"market_id", "token_id" (YES token), "question",
    "market", "event_title"}`` or None when nothing usable is found.
    """
    candidates: list[dict] = []
    try:
        candidates = gamma.search_markets_public(story.title[:120], limit=5) or []
    except Exception as exc:  # noqa: BLE001 - tolerated, recorded
        audit.record(
            "stage_error", {"stage": "gamma.search_markets_public", "error": str(exc)}
        )
    for m in candidates:
        tokens = _market_tokens(m)
        if not tokens:
            continue
        yes_token, _ = tokens
        return {
            "market_id": str(m.get("conditionId") or m.get("id")),
            "token_id": yes_token,
            "question": str(m.get("question") or story.title),
            "market": m,
            "event_title": "",
        }

    # Fallback: event search -> nested markets.
    try:
        events = gamma.search_events(story.title[:120], limit=5) or []
    except Exception as exc:  # noqa: BLE001 - tolerated, recorded
        audit.record(
            "stage_error", {"stage": "gamma.search_events", "error": str(exc)}
        )
        return None
    for ev in events:
        markets = ev.get("markets") or []
        if not markets and ev.get("id"):
            try:
                markets = gamma.get_event(ev["id"]).get("markets", [])
            except Exception as exc:  # noqa: BLE001 - tolerated, recorded
                audit.record(
                    "stage_error", {"stage": "gamma.get_event", "error": str(exc)}
                )
                continue
        for m in markets:
            tokens = _market_tokens(m)
            if not tokens:
                continue
            yes_token, _ = tokens
            return {
                "market_id": str(m.get("conditionId") or m.get("id")),
                "token_id": yes_token,
                "question": str(m.get("question") or ev.get("title") or story.title),
                "market": m,
                "event_title": str(ev.get("title") or ""),
            }
    return None


def _trade_ts(trade: dict) -> float | None:
    """Best-effort unix timestamp from a Data API trade row.

    Handles both row shapes: epoch seconds/millis under ``timestamp`` (v1 and
    v2) and the ISO-8601 ``matchTime`` / ``match_time`` aliases. Only the
    timestamp is read here — USD notional on v2 rows is ``size × price``
    (those rows carry no ``usdc_size``).
    """
    for key in ("timestamp", "matchTime", "match_time", "ts", "time", "createdAt"):
        raw = trade.get(key)
        if raw is None:
            continue
        try:
            ts = float(raw)
        except (TypeError, ValueError):
            # ISO-8601 string?
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        # heuristic: millis vs seconds
        return ts / 1000.0 if ts > 1e12 else ts
    return None


def _latest_trade_ts(rows: list[dict]) -> str:
    """ISO-8601 UTC timestamp of the newest row, or "" when none is readable.

    Args:
        rows: Data API v2 trade rows (any order).

    Returns:
        ISO-8601 UTC string, or "" (honest empty, never a guess).
    """
    latest: float | None = None
    for trade in rows or []:
        ts = _trade_ts(trade)
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    if latest is None:
        return ""
    return datetime.fromtimestamp(latest, tz=timezone.utc).isoformat()


def _snapshots(
    clob: ClobClient,
    data_api: DataApiClient,
    candidates: list[dict],
    audit: AuditLog,
) -> dict[str, dict]:
    """Compose snapshots for many candidate markets with **one** tape call.

    The Data API accepts up to 20 comma-separated condition ids in a single
    ``/v2/trades`` request (live-checked 2026-10-02), so a cycle with several
    candidates no longer pays one tape round-trip per market. Books stay
    per-token (the CLOB batch endpoints are not usable on this deployment —
    see ``ClobClient.get_midpoints``).

    A candidate whose book is unusable is simply absent from the result, and a
    tape failure degrades every ``last_trade_ts`` to "" (the tape is
    informational and never fails a snapshot).

    Args:
        clob: CLOB market-data client.
        data_api: Data API client (v2 only — v1 retired 2026-10-24).
        candidates: ``[{"market_id", "token_id"}, ...]``; duplicates collapse.
        audit: Append-only audit log.

    Returns:
        ``{market_id: snapshot}`` for the candidates that produced one.
    """
    by_market: dict[str, str] = {}
    for cand in candidates or []:
        market_id = str(cand.get("market_id") or "")
        token_id = str(cand.get("token_id") or "")
        if market_id and token_id and market_id not in by_market:
            by_market[market_id] = token_id
    if not by_market:
        return {}

    tapes: dict[str, list[dict]] = {}
    try:
        tapes = data_api.get_trades_v2_many(list(by_market.keys()), limit=5)
    except Exception as exc:  # noqa: BLE001 - the tape is informational here
        audit.record("stage_error", {"stage": "data-api.trades.batch", "error": str(exc)})
        tapes = {}

    out: dict[str, dict] = {}
    for market_id, token_id in by_market.items():
        snap = _snapshot(
            clob,
            data_api,
            market_id,
            token_id,
            audit,
            trade_ts=_latest_trade_ts(tapes.get(market_id, [])),
        )
        if snap is not None:
            out[market_id] = snap
    return out


def _snapshot(
    clob: ClobClient,
    data_api: DataApiClient,
    market_id: str,
    token_id: str,
    audit: AuditLog,
    *,
    trade_ts: str | None = None,
) -> dict | None:
    """Compose the gate's price snapshot from CLOB + Data API primitives.

    * mid / spread (cents) / top-of-book depth from the live order book
      touch; top-N bid/ask levels are kept for the execution book-walk,
    * ``price_ts`` = snapshot time (ISO-8601 UTC),
    * ``last_trade_ts`` = latest Data API **v2** ``/v2/trades?condition=``
      timestamp (audited for transparency; the gate vets news-vs-price skew,
      not trade staleness). The tape is informational: a failure degrades to
      an empty timestamp, never to a failed snapshot.

    Args:
        clob: CLOB market-data client.
        data_api: Data API client (v2 only — v1 retired 2026-10-24).
        market_id: The market's Gamma ``conditionId``; this is what v2's
            ``condition`` filter takes (a token id is NOT accepted).
        token_id: YES CLOB token id (used for the order book).
        audit: Append-only audit log.
        trade_ts: Pre-computed ``last_trade_ts`` (batched path); None makes
            this call fetch the tape itself.
    """
    try:
        book = clob.get_orderbook(token_id) or {}
        # book_levels normalizes the real payload (level objects, string
        # numbers, bids ascending / asks descending = best quote LAST) into
        # best-first pairs, so index 0 is the touch on both sides.
        bids, asks = book_levels(book, BOOK_DEPTH_LEVELS)
        if not bids or not asks:
            raise ValueError("empty book side")
        best_bid_p, best_bid_s = bids[0]
        best_ask_p, best_ask_s = asks[0]
        mid = (best_bid_p + best_ask_p) / 2.0
        spread_cents = (best_ask_p - best_bid_p) * 100.0
        top_depth = best_bid_p * best_bid_s + best_ask_p * best_ask_s
        if trade_ts is None:
            # Single-market path: one tape call for this condition.
            try:
                trades = data_api.get_trades_v2(condition=market_id, limit=5) or []
            except Exception:  # noqa: BLE001 - tape is informational here
                trades = []
            last_trade_ts = _latest_trade_ts(trades)
        else:
            # Batched path: the caller already fetched this market's rows.
            last_trade_ts = trade_ts
    except Exception as exc:  # noqa: BLE001 - tolerated, recorded
        audit.record("stage_error", {"stage": "clob.snapshot", "error": str(exc)})
        return None
    return {
        "mid_price": mid,
        "spread_cents": spread_cents,
        "top_book_depth_usd": top_depth,
        "book_asks": asks,
        "book_bids": bids,
        "price_ts": utcnow_iso(),
        "last_trade_ts": last_trade_ts,
    }


# --------------------------------------------------------------------------- #
# state helpers                                                               #
# --------------------------------------------------------------------------- #
def _load_state() -> dict:
    p = state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_state(state: dict) -> None:
    data_dir()  # ensure exists
    p = state_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(p)


def _keep(items: list, n: int = STATE_LIST_KEEP) -> list:
    return items[-n:]


def _exposure_usd(positions: list[dict]) -> float:
    return sum(
        p["contracts"] * p.get("current_price", p.get("avg_price", 0.0))
        for p in positions
    )


def _apply_fill(positions: list[dict], fill, question: str) -> list[dict]:
    """Merge a fill into the position list (weighted-average entry price)."""
    key = (fill.market_id, fill.token_id, fill.side)
    for p in positions:
        if (p["market_id"], p["token_id"], p["side"]) == key:
            total = p["contracts"] + fill.contracts
            if total > 0:
                p["avg_price"] = (
                    p["avg_price"] * p["contracts"] + fill.price * fill.contracts
                ) / total
            p["contracts"] = total
            p["question"] = question
            return positions
    positions.append(
        {
            "market_id": fill.market_id,
            "token_id": fill.token_id,
            "side": fill.side,
            "contracts": fill.contracts,
            "avg_price": fill.price,
            "current_price": fill.price,
            "unrealized_pnl": 0.0,
            "question": question,
        }
    )
    return positions


def _mark_positions(clob: ClobClient, positions: list[dict]) -> list[dict]:
    """Re-mark every position to the current token price (NO = 1 - YES mid).

    Multi-token price fetch: one batched ``get_midpoints`` call covers every
    position token, falling back per token inside the client when the batch
    endpoints are unavailable (they are, on this deployment — see
    ``ClobClient.get_midpoints``). A token the batch did not cover is fetched
    individually; only a token that fails both ways keeps its last mark.
    """
    by_key = {(p["market_id"], p["token_id"], p["side"]): p for p in positions}
    objs = [
        Position(
            market_id=p["market_id"],
            token_id=p["token_id"],
            side=p["side"],
            contracts=p["contracts"],
            avg_price=p["avg_price"],
            current_price=p.get("current_price", p["avg_price"]),
            unrealized_pnl=p.get("unrealized_pnl", 0.0),
        )
        for p in positions
    ]
    try:
        mids = clob.get_midpoints([p["token_id"] for p in positions]) or {}
    except Exception:  # noqa: BLE001 - per-token fallback below
        mids = {}

    def price_fn(market_id: str, token_id: str, side: str) -> float:
        mid = mids.get(str(token_id))
        if mid is None:
            try:
                mid = float(clob.get_mid_price(token_id))
            except Exception:  # noqa: BLE001 - keep last mark on failure
                return by_key[(market_id, token_id, side)]["avg_price"]
        return mid if side == "YES" else 1.0 - mid

    marked = mark_to_market(objs, price_fn)
    return [
        {
            "market_id": m.market_id,
            "token_id": m.token_id,
            "side": m.side,
            "contracts": m.contracts,
            "avg_price": m.avg_price,
            "current_price": m.current_price,
            "unrealized_pnl": m.unrealized_pnl,
            "question": by_key[(m.market_id, m.token_id, m.side)].get("question", ""),
        }
        for m in marked
    ]


# --------------------------------------------------------------------------- #
# main cycle                                                                  #
# --------------------------------------------------------------------------- #
def _fetch_stories(settings: Settings, audit: AuditLog) -> list[Story]:
    """Aggregate stories from all enabled sources; failures -> [] (honest).

    Sources: Reddit (optional, needs pre-approval), RSS news (Google News
    + outlets, keyless), GDELT (keyless). X is excluded by design — see
    app/scraper/twitter.py.
    """
    stories: list[Story] = []
    for name, fetcher in (
        ("reddit", fetch_reddit_trending),
        ("news", fetch_news_trending),
        ("gdelt", fetch_gdelt_trending),
    ):
        try:
            stories.extend(fetcher(settings) or [])
        except Exception as exc:  # noqa: BLE001 - tolerated, recorded
            audit.record(
                "stage_error", {"stage": f"fetch_{name}", "error": str(exc)}
            )
    stories = dedupe_by_url(stories)
    return filter_by_engagement(stories)


def run_once(settings: Settings) -> dict:
    """Run one full paper-trading cycle.

    Args:
        settings: App Settings.

    Returns:
        JSON-serializable summary dict (also recorded as ``cycle_end``).
    """
    ks = read_killswitch()
    if ks.get("engaged"):
        return {
            "status": "halted",
            "mode": "PAPER",
            "reason": "kill_switch_engaged",
            "kill_switch": ks,
            "note": "paper loop halted by kill switch",
        }

    audit = AuditLog(resolve_db_path(settings.DATABASE_PATH))
    gate = RiskGate(settings)
    engine = None  # constructed lazily with the other clients
    gamma = clob = data_api = jev = None

    audit.record("cycle_start", {"mode": "PAPER", "ts": utcnow_iso()})

    state = _load_state()
    bankroll = float(settings.PAPER_BANKROLL_USD)
    cash = float(state.get("cash_usd", bankroll))
    positions: list[dict] = state.get("positions", [])

    stories = _fetch_stories(settings, audit)

    n_signals = n_approved = n_vetoed = 0
    run_signals: list[dict] = []
    run_vetoes: list[dict] = []
    run_fills: list[dict] = []

    if stories:
        # Lazy clients: no network objects until there is work to do, so the
        # empty-feed offline path stays completely offline.
        engine = PaperEngine(settings, audit)
        gamma = GammaClient(settings)
        clob = ClobClient(settings)
        data_api = DataApiClient(settings)
        jev = JevClient(settings)

    try:
        # Match every story first, then take all snapshots in one pass so the
        # tape costs one batched /v2/trades call per cycle instead of one per
        # candidate (the books stay per-token — see _snapshots).
        planned: list[tuple[Story, str, dict]] = []
        for story in stories[:MAX_STORIES_PER_CYCLE]:
            try:
                summary = summarize(story)
            except Exception as exc:  # noqa: BLE001 - tolerated
                audit.record(
                    "stage_error", {"stage": "summarize", "error": str(exc)}
                )
                summary = story.title

            picked = _pick_market(gamma, story, audit)
            if picked is None:
                continue
            planned.append((story, summary, picked))

        snapshots = _snapshots(
            clob,
            data_api,
            [
                {
                    "market_id": picked["market_id"],
                    "token_id": picked["token_id"],
                }
                for _, _, picked in planned
            ],
            audit,
        )

        for story, summary, picked in planned:
            snap = snapshots.get(str(picked["market_id"]))
            if snap is None:
                continue

            question = picked["question"]
            market = picked["market"]
            try:
                decision = jev.decide(
                    DecisionState(
                        news_summary=summary,
                        market_question=question,
                        yes_price=snap["mid_price"],
                    )
                )
            except Exception as exc:  # noqa: BLE001 - tolerated, recorded
                audit.record(
                    "stage_error", {"stage": "jev.decide", "error": str(exc)}
                )
                continue
            p_true = float(decision.p_true)

            signal = Signal(
                market_id=picked["market_id"],
                token_id=picked["token_id"],
                question=question,
                p_true=p_true,
                market_price=snap["mid_price"],
                spread_cents=snap["spread_cents"],
                top_book_depth_usd=snap["top_book_depth_usd"],
                hours_to_resolution=_hours_to_resolution(market),
                news_ts=story.published_at or "",
                price_ts=snap["price_ts"],
                negrisk_sum=negrisk_sum(market),
                uma_dispute=_uma_dispute(market),
                jev_confidence=decision.jev_confidence,
                jev_choice=decision.choice,
                fee_category=_fee_category(market, picked["event_title"]),
                book_asks=snap["book_asks"],
                book_bids=snap["book_bids"],
            )
            n_signals += 1
            audit.record(
                "signal",
                {
                    **asdict(signal),
                    "model_choice": decision.choice,
                    "model_confidence": decision.jev_confidence,
                    "model_version": decision.jev_model,  # audit pins the model
                    "model_mock": decision.mock,
                    "story_source": story.source,
                    "last_trade_ts": snap["last_trade_ts"],
                },
            )
            run_signals.append({**asdict(signal), "model_mock": decision.mock})

            result = gate.evaluate(
                signal,
                _exposure_usd(positions),
                bankroll,
                open_positions_count=len(positions),
            )

            if not result.approved:
                n_vetoed += 1
                for v in result.vetoes:
                    entry = {
                        "market_id": signal.market_id,
                        "question": signal.question,
                        "reason": v.reason,
                        "detail": v.detail,
                        "ts": utcnow_iso(),
                    }
                    audit.record("veto", entry)
                    run_vetoes.append(entry)
                continue

            n_approved += 1
            fill = engine.execute(
                signal,
                result.size_usd,
                fee_category=signal.fee_category,
                fee_rate_override=_fee_rate_override(market, settings),
            )
            cash -= fill.filled_usd + fill.fee_usd + fill.slippage_usd
            positions = _apply_fill(positions, fill, question)
            run_fills.append(asdict(fill))
    finally:
        for client in (gamma, clob, data_api, jev):
            try:
                if client is not None:
                    client.close()
            except Exception:  # noqa: BLE001 - best effort
                pass

    if clob is not None and positions:
        # Re-mark with a fresh short-lived client (the one above is closed).
        _clob = ClobClient(settings)
        try:
            positions = _mark_positions(_clob, positions)
        finally:
            _clob.close()

    exposure = _exposure_usd(positions)
    equity = cash + exposure
    drawdown_pct = (
        max(0.0, (bankroll - equity) / bankroll * 100.0) if bankroll > 0 else 0.0
    )

    kill_engaged = False
    if drawdown_pct >= float(settings.KILL_DRAWDOWN_PCT):
        record = write_killswitch(
            True,
            f"auto: paper drawdown {drawdown_pct:.2f}% >= "
            f"KILL_DRAWDOWN_PCT {settings.KILL_DRAWDOWN_PCT}%",
        )
        audit.record("killswitch", {"auto": True, "reason": record["reason"]})
        kill_engaged = True

    new_state = {
        "updated_at": utcnow_iso(),
        "mode": "PAPER",
        "bankroll_usd": bankroll,
        "cash_usd": round(cash, 2),
        "exposure_usd": round(exposure, 2),
        "equity_usd": round(equity, 2),
        "drawdown_pct": round(drawdown_pct, 4),
        "kill_switch": read_killswitch(),
        "positions": positions,
        "signals": _keep(state.get("signals", []) + run_signals),
        "vetoes": _keep(state.get("vetoes", []) + run_vetoes),
        "fills": _keep(state.get("fills", []) + run_fills),
        "calibration": state.get(
            "calibration",
            {"note": "paper mode: no resolutions observed yet", "bins": []},
        ),
    }
    _write_state(new_state)

    summary = {
        "status": "ok",
        "mode": "PAPER",
        "stories_seen": len(stories),
        "signals": n_signals,
        "approved": n_approved,
        "vetoed": n_vetoed,
        "fills": len(run_fills),
        "cash_usd": round(cash, 2),
        "equity_usd": round(equity, 2),
        "drawdown_pct": round(drawdown_pct, 4),
        "kill_switch_engaged": kill_engaged,
    }
    if n_signals == 0:
        summary["note"] = "no opportunities"
    audit.record("cycle_end", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: ``--once`` (default) or ``--watch``."""
    parser = argparse.ArgumentParser(
        description="hunchfall paper trading loop (no real orders, ever)"
    )
    parser.add_argument("--once", action="store_true", help="run one cycle")
    parser.add_argument(
        "--watch",
        action="store_true",
        help="loop every LOOP_INTERVAL_SEC until kill switch / Ctrl-C",
    )
    args = parser.parse_args(argv)
    settings = Settings()

    if args.watch:
        # NOTE: --watch polls REST each cycle. app/polymarket/ws.py is the
        # optional websocket upgrade (live book/price_change events +
        # staleness heartbeat, no extra REST polling) — wire it into the
        # snapshot path here when going live.
        interval = int(settings.LOOP_INTERVAL_SEC)
        try:
            while True:
                ks = read_killswitch()
                if ks.get("engaged"):
                    print(
                        json.dumps(
                            {
                                "status": "halted",
                                "mode": "PAPER",
                                "reason": "kill_switch_engaged",
                            }
                        )
                    )
                    return 0
                try:
                    summary = run_once(settings)
                except Exception as exc:  # noqa: BLE001 - keep watching
                    summary = {"status": "error", "mode": "PAPER", "error": str(exc)}
                print(json.dumps(summary, indent=2), flush=True)
                time.sleep(interval)
        except KeyboardInterrupt:
            print(json.dumps({"status": "stopped", "mode": "PAPER"}))
            return 0

    print(json.dumps(run_once(settings), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
