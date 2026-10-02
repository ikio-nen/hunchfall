"""Market guesser service (RFC-003) — prediction only.

Given a Polymarket market, produce a **paper prediction**: P(YES), a direction,
a confidence, and typed reasons — or an honest abstention when the model adds
less than ``PREDICT_ABSTAIN_EDGE`` over the market price.

Pipeline::

    resolve_market()        Gamma only (slug verified; condition_id live-checked)
    CLOB book + midpoint    best bid/ask, spread, top-N depth, last_trade_price  (required)
    /v2/trades tape         USD = size x price                                   (feature)
    social pulse            Jetstream window + Reddit + RSS, shared deadline     (feature)
    feature build           deterministic; canonical JSON -> sha256              (pure)
    model                   Jev decide() x N -> median p_yes, disagreement | MOCK
    decision policy         edge, direction, abstention, confidence, reasons     (pure)
    audit: predict event    append-only ledger
    -> 200 prediction (or an abstention)

Hard rules (RFC-003 invariants):

* **Prediction only.** This module never imports ``PaperEngine``,
  ``app.execution``, or ``app.loop`` (enforced by
  ``tests/test_scan_prediction_only.py``); it writes no ``fill`` events and
  never constructs the paper engine.
* **Nothing here reads ``state.json``, the gate, exposure, or bankroll** — a
  prediction is information, not a trade intent.
* Gamma + CLOB are **required** (failure -> 502, nothing persisted, fail
  closed). The tape and the social pulse are **features**: unavailable ->
  omitted, listed in ``snapshot.missing``, and named in ``reasons``.
* **Mock is labelled.** The mock path reports ``model.mock=true``,
  ``version="jev-mock"``, and the note ``MOCK — not a real model``.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from app.config import Settings
from app.jev.client import DecisionState, JevClient
from app.memory.audit import AuditLog
from app.paths import utcnow_iso
from app.polymarket.clob import ClobClient, book_levels
from app.polymarket.data_api import DataApiClient, DataApiError
from app.polymarket.gamma import (
    GammaClient,
    GammaError,
    is_resolved,
    parse_token_ids,
)
from app.social import SocialAnalyzer

#: The badge every prediction surface must carry.
PAPER_LABEL = "paper prediction · no trade placed"
#: The mock label — never a real model.
MOCK_NOTE = "MOCK — not a real model"
#: The mock model version string.
MOCK_VERSION = "jev-mock"

#: Momentum/acceleration windows, in seconds.
_W15 = 15 * 60
_W60 = 60 * 60
_W135 = 135 * 60

#: Saturation for the snapshot text handed to the model (bytes).
SNAPSHOT_TEXT_MAX = 2048


class PredictError(Exception):
    """Base prediction error carrying the HTTP status and machine code."""

    status = 500
    code = "predict_error"

    def __init__(self, message: str, *, source: str | None = None) -> None:
        """Store the message and optional upstream source name."""
        super().__init__(message)
        self.message = message
        self.source = source


class PredictBadRequest(PredictError):
    """Malformed prediction request (400)."""

    status = 400
    code = "bad_request"


class PredictNotFound(PredictError):
    """Gamma has no market for the identifier (404)."""

    status = 404
    code = "market_not_found"


class PredictNotTradeable(PredictError):
    """Closed/resolved market or missing token pair (409)."""

    status = 409
    code = "market_not_tradeable"


class PredictUpstreamError(PredictError):
    """Gamma/CLOB/Data API/Jev failure — nothing persisted (502)."""

    status = 502
    code = "upstream_error"


# ------------------------------------------------------------------ helpers --
def _float(value: Any) -> float | None:
    """Coerce a value to float; None when not numeric."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first(item: dict, keys: tuple[str, ...], default: Any = None) -> Any:
    """First present, non-None value among ``keys``."""
    for key in keys:
        value = item.get(key)
        if value is not None:
            return value
    return default


def _epoch_seconds(value: Any) -> float | None:
    """Best-effort epoch seconds from an epoch or ISO timestamp value."""
    numeric = _float(value)
    if numeric is not None:
        return numeric / 1000.0 if numeric > 1e12 else numeric
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _round(value: float, places: int) -> float:
    """Round a float to ``places`` decimals."""
    return round(float(value), places)


def _clamp01(value: float) -> float:
    """Clamp a float into [0, 1]."""
    return max(0.0, min(1.0, float(value)))


def _usd(value: Any) -> str:
    """Format a USD amount with thousands separators (\u201c18,400\u201d).

    Used only for human-readable reason strings; the numeric payload keeps
    full precision.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:,.0f}" if number.is_integer() else f"{number:,.2f}"


def hours_to_resolution(market: dict, now_ts: float | None = None) -> float | None:
    """Hours until Gamma ``endDate``; None when unparseable (local date math).

    Deliberately implemented here rather than imported from ``app.loop``: the
    prediction path must not import the trading loop.

    Args:
        market: Gamma-shaped market dict.
        now_ts: Optional "now" epoch seconds (tests).

    Returns:
        Hours to resolution, or None.
    """
    raw = market.get("endDate") or market.get("end_date")
    end = _epoch_seconds(raw)
    if end is None:
        return None
    now = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
    return (end - now) / 3600.0


def volume24hr_usd(market: dict) -> float | None:
    """Gamma 24h volume in USD (defensive aliases); None when absent."""
    return _float(
        _first(market, ("volume24hr", "volume24Hr", "volume_24hr", "volume24h"))
    )


# ------------------------------------------------------------------ features --
def book_features(book: dict, levels: int) -> dict:
    """Deterministic book features from a CLOB book payload.

    Levels are normalized by ``clob.book_levels``: the real ``/book`` payload
    serves level *objects* with string numbers, bids ascending and asks
    descending, so index 0 is the WORST quote (live-checked 2026-10-01 —
    reading it gave a 50.0¢ mid for a 2.85¢ market).

    A genuinely empty book is a required-input failure (502). A **one-sided**
    book (one half empty — normal for a near-resolution market) is handled
    honestly: the available side's best price and depth are kept,
    ``imbalance``/``spread_cents``/``market_mid`` are omitted (computing them
    would fabricate the missing side), and ``one_sided`` names the empty side
    so the caller can abstain on data quality and resolve the mid another way.

    Args:
        book: CLOB ``/book`` payload.
        levels: Top-N levels per side used for depth.

    Returns:
        Feature dict with the required book fields; ``one_sided`` (the empty
        side) only when one half is empty.

    Raises:
        PredictUpstreamError: when the book is empty (required input) or the
            touch is unusable.
    """
    bids, asks = book_levels(book if isinstance(book, dict) else {}, levels)
    if not bids and not asks:
        raise PredictUpstreamError("CLOB order book empty", source="clob")
    one_sided = "ask" if not asks else ("bid" if not bids else None)
    out: dict = {}
    bid_depth = sum(price * size for price, size in bids)
    ask_depth = sum(price * size for price, size in asks)
    if bids:
        best_bid = bids[0][0]
        if best_bid <= 0.0:
            raise PredictUpstreamError(
                "CLOB book has no usable touch", source="clob"
            )
        out["best_bid"] = _round(best_bid, 6)
        out["bid_depth_usd"] = _round(bid_depth, 2)
    if asks:
        best_ask = asks[0][0]
        if best_ask <= 0.0:
            raise PredictUpstreamError(
                "CLOB book has no usable touch", source="clob"
            )
        out["best_ask"] = _round(best_ask, 6)
        out["ask_depth_usd"] = _round(ask_depth, 2)
    if one_sided is not None:
        out["one_sided"] = one_sided
    else:
        total = bid_depth + ask_depth
        out["market_mid"] = _round((bids[0][0] + asks[0][0]) / 2.0, 6)
        out["spread_cents"] = _round((asks[0][0] - bids[0][0]) * 100.0, 4)
        out["imbalance"] = (
            _round((bid_depth - ask_depth) / total, 4) if total > 0 else 0.0
        )
    last = _float(book.get("last_trade_price"))
    if last is not None:
        out["last_trade_price"] = _round(last, 6)
    return out


def price_history_features(
    history: list[dict],
    *,
    now_ts: float | None = None,
    change_1h_sec: int = 3600,
    change_24h_sec: int = 86400,
) -> dict:
    """Deterministic 1h/24h change + realized volatility from ``/prices-history``.

    The history is the CLOB ``/prices-history`` payload — points shaped
    ``{"t": epoch_seconds, "p": price}`` — keyed by the YES token id, so the
    prices are already market-implied P(YES) (no mirroring). The predictor
    previously derived momentum *only* from the trade tape; this is the same
    question asked of the price series instead.

    All three features are **model features, not vetoes**: they ride in the
    snapshot and can inform direction, but they never set abstention — that
    stays edge-based (``PREDICT_ABSTAIN_EDGE``). Keys are omitted when the
    history is too short to compute them (never guessed).

    Args:
        history: CLOB price-history points (any order).
        now_ts: Optional "now" epoch seconds (tests). Defaults to the newest
            point in the series, so a stale history still measures the move
            *within* the series instead of comparing against wall-clock.
        change_1h_sec: Lookback for the 1h change.
        change_24h_sec: Lookback for the 24h change.

    Returns:
        ``{"last", "change_1h", "change_24h", "realized_vol"}``, each key
        present only when computable; an empty dict for an unusable history.
    """
    points: list[tuple[float, float]] = []
    for item in history or []:
        if not isinstance(item, dict):
            continue
        ts = _float(_first(item, ("t", "timestamp", "time")))
        price = _float(_first(item, ("p", "price")))
        if ts is None or price is None:
            continue
        if not 0.0 < price < 1.0:
            continue
        points.append((ts, price))
    if not points:
        return {}
    points.sort(key=lambda pair: pair[0])
    last_ts, last_price = points[-1]
    reference = now_ts if now_ts is not None else last_ts
    out: dict = {"last": _round(last_price, 6), "points": len(points)}

    def _at_or_before(cutoff: float) -> tuple[float, float] | None:
        """Newest ``(ts, price)`` at or before ``cutoff``.

        Returns None when the series is younger than the cutoff, and the
        caller additionally requires the point to be **strictly older** than
        the last one — otherwise a single-point series would report a fake
        "no change" instead of an honest unknown.
        """
        picked: tuple[float, float] | None = None
        for ts, price in points:
            if ts <= cutoff:
                picked = (ts, price)
            else:
                break
        return picked

    for label, lookback in (
        ("change_1h", change_1h_sec),
        ("change_24h", change_24h_sec),
    ):
        prior = _at_or_before(reference - lookback)
        if prior is not None and prior[0] < last_ts and prior[1] > 0.0:
            out[label] = _round(last_price - prior[1], 6)

    recent = [
        price for ts, price in points if reference - change_24h_sec <= ts <= reference
    ]
    if len(recent) >= 3:
        mean = sum(recent) / len(recent)
        variance = sum((price - mean) ** 2 for price in recent) / (len(recent) - 1)
        out["realized_vol"] = _round(variance**0.5, 6)
    return out


def _window(rows: list[tuple[float, float, str]], now: float) -> dict:
    """VWAP/USD statistics for a set of ``(ts, usd, side)`` trade rows."""
    size_price = sum(usd for _, usd, _ in rows)
    buy = sum(usd for _, usd, side in rows if side == "BUY")
    sell = sum(usd for _, usd, side in rows if side == "SELL")
    return {"usd": size_price, "buy": buy, "sell": sell, "n": len(rows)}


def _is_yes_side_row(row: Any, yes_token_id: str) -> bool:
    """True when a ``/v2/trades`` row belongs to the market's YES token.

    The endpoint interleaves **both outcomes** (live-checked 2026-10-01: 71 of
    the last 100 rows for one market were NO-token trades near 0.97 while the
    YES token traded at 0.029), so every USD/VWAP feature must first be scoped
    to a single token. ``token_id`` is authoritative; ``outcome_index`` and
    ``outcome`` are accepted as aliases. A row that identifies neither is
    dropped — counting it would corrupt the flow numbers.

    Args:
        row: One trade row.
        yes_token_id: The YES token id to keep.

    Returns:
        True when the row is a YES-token trade.
    """
    if not isinstance(row, dict):
        return False
    token = row.get("token_id")
    if token is not None:
        return str(token) == str(yes_token_id)
    index = row.get("outcome_index")
    if isinstance(index, int):
        return index == 0
    return str(row.get("outcome") or "").strip().lower() in ("yes", "true", "1")


def tape_features(
    rows: list[dict],
    now_ts: float | None = None,
    yes_token_id: str | None = None,
) -> dict:
    """Deterministic tape features from ``/v2/trades`` rows.

    **USD notional per trade is ``size × price``** — these rows carry no
    ``usdc_size`` (see docs/API_INVENTORY.md); the formula is pinned by test.

    **Only the YES token's trades are counted.** The feed interleaves both
    outcomes, and blending them produced a vwap of 0.7628 for a market
    trading at 2.85¢ with a flow imbalance that was really the NO side's
    buying (live-checked 2026-10-01).

    Args:
        rows: Data API v2 trade items.
        now_ts: Optional "now" epoch seconds (tests).
        yes_token_id: The market's YES token id; rows for the other outcome
            are dropped. None keeps every row (unit tests only).

    Returns:
        Tape feature dict; momentum/accel keys are omitted when a window is
        empty (never guessed).
    """
    now = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
    if yes_token_id is not None:
        rows = [row for row in rows or [] if _is_yes_side_row(row, yes_token_id)]
    parsed: list[tuple[float, float, str, float]] = []
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        price = _float(_first(item, ("price",)))
        size = _float(_first(item, ("size", "shares")))
        ts = _epoch_seconds(
            _first(item, ("timestamp", "matchTime", "match_time", "ts", "time"))
        )
        if price is None or size is None or ts is None:
            continue
        side = str(_first(item, ("side", "takerSide"), "") or "").upper()
        parsed.append((ts, size * price, side, size))  # USD = size x price

    count = len(parsed)
    buy = sum(usd for _, usd, side, _ in parsed if side == "BUY")
    sell = sum(usd for _, usd, side, _ in parsed if side == "SELL")
    total = buy + sell
    out: dict = {
        "count": count,
        "buy_usd": _round(buy, 2),
        "sell_usd": _round(sell, 2),
        "flow_imbalance": _round((buy - sell) / total, 4) if total > 0 else 0.0,
    }

    shares = sum(size for _, _, _, size in parsed)
    if count and shares > 0:
        out["vwap"] = _round(sum(usd for _, usd, _, _ in parsed) / shares, 6)

    def _vwap_between(lo_ago: int, hi_ago: int) -> float | None:
        picked = [
            (usd, size)
            for ts, usd, _, size in parsed
            if (now - hi_ago) <= ts < (now - lo_ago)
        ]
        if not picked:
            return None
        share_total = sum(size for _, size in picked)
        if share_total <= 0:
            return None
        return sum(usd for usd, _ in picked) / share_total

    def _usd_between(lo_ago: int, hi_ago: int) -> float:
        return sum(
            usd for ts, usd, _, _ in parsed if (now - hi_ago) <= ts < (now - lo_ago)
        )

    for label, window in (("momentum_15m", _W15), ("momentum_60m", _W60)):
        recent = _vwap_between(0, window)
        prior = _vwap_between(window, 2 * window)
        if recent is not None and prior is not None:
            out[label] = _round(recent - prior, 6)

    baseline = _usd_between(_W15, _W135) / 8.0
    if baseline > 0:
        out["volume_accel"] = _round(_usd_between(0, _W15) / baseline, 4)
    return out


def build_snapshot_text(
    question: str, market_mid: float, features: dict, missing: list[str]
) -> str:
    """Compact state text handed to the model (<= 2 KB, control chars stripped).

    Args:
        question: Market question.
        market_mid: CLOB midpoint (the market-implied base rate).
        features: Book/tape/price-history/social features.
        missing: Names of unavailable feature sources.

    Returns:
        Compact multi-line text.
    """
    lines = [f"Market: {question}", f"Market-implied base rate (YES mid): {market_mid:.3f}"]
    if features.get("one_sided"):
        depth_bits = [
            f"${features[key]} {name}"
            for key, name in (("bid_depth_usd", "bid"), ("ask_depth_usd", "ask"))
            if key in features
        ]
        lines.append(
            f"Book: one-sided ({features['one_sided']} side empty) · "
            f"mid {features.get('market_mid')} via /{features.get('mid_source')}"
            + (f" · depth {' / '.join(depth_bits)}" if depth_bits else "")
        )
    elif "best_bid" in features:
        lines.append(
            f"Book: bid {features['best_bid']} / ask {features['best_ask']} · "
            f"spread {features.get('spread_cents')}¢ · "
            f"depth ${features.get('bid_depth_usd')} bid / "
            f"${features.get('ask_depth_usd')} ask · "
            f"imbalance {features.get('imbalance'):+.2f}"
        )
    tape = features.get("tape")
    if isinstance(tape, dict) and tape.get("count"):
        lines.append(
            f"Tape ({tape['count']} trades): buy ${_usd(tape.get('buy_usd'))} / "
            f"sell ${_usd(tape.get('sell_usd'))} · flow {tape.get('flow_imbalance'):+.2f}"
            + (f" · vwap {tape['vwap']}" if "vwap" in tape else "")
        )
        momentum_bits = [
            f"{key.replace('momentum_', '')} {tape[key]:+.4f}"
            for key in ("momentum_15m", "momentum_60m")
            if key in tape
        ]
        if "volume_accel" in tape:
            momentum_bits.append(f"volume_accel {tape['volume_accel']}x")
        if momentum_bits:
            lines.append("Momentum: " + " · ".join(momentum_bits))
    history = features.get("price_history")
    if isinstance(history, dict) and history:
        bits = []
        if "change_1h" in history:
            bits.append(f"1h {history['change_1h']:+.4f}")
        if "change_24h" in history:
            bits.append(f"24h {history['change_24h']:+.4f}")
        if "realized_vol" in history:
            bits.append(f"realized_vol {history['realized_vol']:.4f}")
        if bits:
            lines.append("Price history: " + " · ".join(bits))
    if "hours_to_resolution" in features:
        line = f"Resolution in {features['hours_to_resolution']}h"
        if "volume24hr_usd" in features:
            line += f" · 24h volume ${features['volume24hr_usd']}"
        lines.append(line)
    social = features.get("social")
    if isinstance(social, dict) and social.get("platforms_ok"):
        label = "proxy for X" if social.get("proxy") else social.get("relevance", "")
        lines.append(
            f"Social ({' + '.join(social['platforms_ok'])} · {label}): "
            f"{social.get('posts_window', 0)} posts · "
            f"{social.get('engagement_window', 0)} engagements"
            + (f" · velocity {social['velocity']}x" if "velocity" in social else "")
            + " [network-wide (unfiltered) — the Jetstream tail is not "
            "keyword-filtered, so these are network counts, not subject counts]"
        )
        item = social.get("top_item")
        if isinstance(item, dict) and item.get("text"):
            lines.append(f"Top item ({item.get('platform')}): {item['text']}")
    if missing:
        lines.append("Missing feature sources: " + ", ".join(missing))
    text = "\n".join(lines)
    text = "".join(ch for ch in text if ch.isprintable() or ch == "\n")
    return text[:SNAPSHOT_TEXT_MAX]


# ------------------------------------------------------------------- policy --
def decide(
    p_yes: float, market_price: float, threshold: float, disagreement: float
) -> dict:
    """Deterministic decision policy (pure).

    Args:
        p_yes: Median model P(YES).
        market_price: Market-implied YES price (``market_mid``).
        threshold: ``PREDICT_ABSTAIN_EDGE``.
        disagreement: Ensemble spread (max - min).

    Returns:
        ``{"edge_vs_market", "direction", "abstained", "confidence"}``.
    """
    edge = _round(p_yes - market_price, 4)
    if edge >= threshold:
        direction = "YES"
    elif edge <= -threshold:
        direction = "NO"
    else:
        direction = "ABSTAIN"
    confidence = _round(
        min(1.0, abs(2.0 * p_yes - 1.0) * (1.0 - min(1.0, disagreement))), 4
    )
    return {
        "edge_vs_market": edge,
        "direction": direction,
        "abstained": direction == "ABSTAIN",
        "confidence": confidence,
    }


def _force_data_quality_abstention(features: dict, decision: dict) -> dict:
    """Force ABSTAIN when the snapshot's book is one-sided (pure).

    A one-sided book means the mid came from the fallback chain, not from a
    live two-sided touch — no guess computed against it may be trusted. The
    computed p_yes/edge/confidence stay in the payload (schema unchanged);
    only the verdict is overridden to a data-quality abstention.

    Args:
        features: Snapshot features (read ``one_sided``).
        decision: ``decide()`` output (mutated in place).

    Returns:
        The same decision dict, possibly forced to ABSTAIN.
    """
    if features.get("one_sided"):
        decision["direction"] = "ABSTAIN"
        decision["abstained"] = True
    return decision


def build_reasons(
    features: dict, decision: dict, missing: list[str], threshold: float
) -> list[str]:
    """Typed reason strings only — never model prose (>=1 always).

    Args:
        features: Snapshot features.
        decision: Decision policy output.
        missing: Names of unavailable feature sources.
        threshold: ``PREDICT_ABSTAIN_EDGE``.

    Returns:
        List of reason strings.
    """
    reasons: list[str] = []
    if "imbalance" in features:
        side = "bids" if features["imbalance"] >= 0 else "asks"
        reasons.append(
            f"book_imbalance {features['imbalance']:+.2f} ({side} dominate top levels)"
        )
    tape = features.get("tape")
    if isinstance(tape, dict) and tape.get("count"):
        reasons.append(
            f"flow_imbalance {tape.get('flow_imbalance'):+.2f} over "
            f"{tape['count']} trades (${_usd(tape.get('buy_usd'))} buy / "
            f"${_usd(tape.get('sell_usd'))} sell)"
        )
        if "momentum_60m" in tape:
            reasons.append(f"momentum_60m {tape['momentum_60m']:+.4f}")
        if "volume_accel" in tape:
            reasons.append(
                f"volume_accel {tape['volume_accel']}x (last 15m vs prior 2h rate)"
            )
    history = features.get("price_history")
    if isinstance(history, dict):
        if "change_24h" in history:
            reasons.append(
                f"price_history_24h {history['change_24h']:+.4f} "
                "(CLOB /prices-history, feature not veto)"
            )
        if "change_1h" in history:
            reasons.append(f"price_history_1h {history['change_1h']:+.4f}")
        if "realized_vol" in history:
            reasons.append(
                f"realized_vol {history['realized_vol']:.4f} over 24h "
                "(CLOB /prices-history)"
            )
    social = features.get("social")
    if isinstance(social, dict) and social.get("platforms_ok"):
        kind = "proxy" if social.get("proxy") else social.get("relevance", "")
        reasons.append(
            f"social pulse: {social.get('posts_window', 0)} posts / "
            f"{social.get('engagement_window', 0)} engagements on "
            f"{' + '.join(social['platforms_ok'])} ({kind}) — network-wide "
            f"(unfiltered), not subject-filtered"
        )
    if "market_mid" in features:
        reasons.append(f"base_rate anchor: market-implied {features['market_mid']}")
    for name in missing:
        reasons.append(f"missing feature source: {name}")
    if features.get("one_sided"):
        reasons.append(
            f"abstained: one-sided book (no {features['one_sided']} side) — "
            f"data-quality abstention (mid {features.get('market_mid')} via "
            f"/{features.get('mid_source') or 'book'})"
        )
    elif decision["abstained"]:
        reasons.append(
            f"abstained: |edge {abs(decision['edge_vs_market'])}| < "
            f"threshold {threshold:.3f}"
        )
    else:
        reasons.append(
            f"edge {decision['edge_vs_market']:+.3f} >= threshold "
            f"{threshold:.3f} → {decision['direction']}"
        )
    return reasons or ["no features available"]


# -------------------------------------------------------------------- ledger --
def accuracy_report(
    predictions: list[dict],
    resolutions: list[dict],
    *,
    include_mock: bool = False,
    limit: int = 500,
) -> dict:
    """Derived-on-read calibration ledger (recomputed per request).

    Args:
        predictions: ``predict`` event dicts (any order).
        resolutions: ``predict_resolved`` event dicts (any order).
        include_mock: Count mock predictions in the headline metrics.
        limit: Max predictions scanned (newest first by id).

    Returns:
        The ``/predict/accuracy`` payload body (without ``mode``/``mode`` keys).
    """
    ordered = sorted(predictions, key=lambda e: e.get("id") or 0, reverse=True)[:limit]

    latest: dict[str, dict] = {}
    for event in resolutions:
        payload = event.get("payload") or {}
        pid = str(payload.get("prediction_id") or "")
        if not pid:
            continue
        previous = latest.get(pid)
        if previous is None or (event.get("id") or 0) > (previous.get("id") or 0):
            latest[pid] = event

    def _is_mock(payload: dict) -> bool:
        model = payload.get("model") or {}
        return bool(model.get("mock")) if isinstance(model, dict) else False

    rows: list[dict] = []
    mock_logged = mock_resolved = live_logged = live_resolved = 0
    for event in ordered:
        payload = event.get("payload") or {}
        is_mock = _is_mock(payload)
        pid = str(payload.get("prediction_id") or "")
        resolution = latest.get(pid)
        resolved = resolution is not None
        if is_mock:
            mock_logged += 1
            mock_resolved += 1 if resolved else 0
        else:
            live_logged += 1
            live_resolved += 1 if resolved else 0
        rows.append({"payload": payload, "mock": is_mock, "resolution": resolution})

    scoped = [row for row in rows if include_mock or not row["mock"]]

    def _outcome(row: dict) -> float | None:
        if row["resolution"] is None:
            return None
        outcome = str((row["resolution"].get("payload") or {}).get("outcome") or "").upper()
        if outcome == "YES":
            return 1.0
        if outcome == "NO":
            return 0.0
        return None

    scored: list[tuple[float, float, float, float]] = []  # p, market, outcome, ...
    guessed: list[tuple[float, float, float]] = []
    abstained = 0
    for row in scoped:
        payload = row["payload"]
        p_yes = _float(payload.get("p_yes"))
        market = _float(payload.get("market_price"))
        if payload.get("abstained"):
            abstained += 1
        outcome = _outcome(row)
        if p_yes is None or market is None or outcome is None:
            continue
        scored.append((p_yes, market, outcome, 1.0))
        if not payload.get("abstained"):
            guessed.append((p_yes, market, outcome))

    def _brier(pairs: list[tuple[float, float, float]]) -> dict | None:
        if not pairs:
            return None
        model = sum((p - o) ** 2 for p, _, o in pairs) / len(pairs)
        market = sum((m - o) ** 2 for _, m, o in pairs) / len(pairs)
        baseline = 0.25
        return {
            "model": _round(model, 4),
            "market": _round(market, 4),
            "baseline_0_5": baseline,
            "skill_vs_market": (
                _round(1.0 - model / market, 4) if market > 0 else None
            ),
            "skill_vs_baseline": _round(1.0 - model / baseline, 4),
        }

    scored_pairs = [(p, m, o) for p, m, o, _ in scored]
    guessed_pairs = [(p, m, o) for p, m, o in guessed]
    brier = _brier(scored_pairs)
    brier_guessed = _brier(guessed_pairs) if guessed_pairs else None
    if brier_guessed is not None:
        brier_guessed["n"] = len(guessed_pairs)

    direction_correct = 0
    for p_yes, market, outcome in guessed_pairs:
        direction = "YES" if p_yes - market >= 0 else "NO"
        if (direction == "YES" and outcome == 1.0) or (
            direction == "NO" and outcome == 0.0
        ):
            direction_correct += 1

    bins: list[dict] = []
    for index in range(10):
        lo, hi = index / 10.0, (index + 1) / 10.0
        in_bin = [
            (p, o)
            for p, _, o, _ in scored
            if (lo <= p < hi) or (index == 9 and p == 1.0)
        ]
        count = len(in_bin)
        bins.append(
            {
                "lo": _round(lo, 1),
                "hi": _round(hi, 1),
                "n": count,
                "mean_p": _round(sum(p for p, _ in in_bin) / count, 4) if count else None,
                "event_rate": (
                    _round(sum(o for _, o in in_bin) / count, 4) if count else None
                ),
            }
        )

    logged_guessed = [row for row in scoped if not row["payload"].get("abstained")]
    abs_edges = [
        abs(_float(row["payload"].get("edge_vs_market")) or 0.0) for row in logged_guessed
    ]
    return {
        "n_logged": len(scoped),
        "n_resolved": len(scored),
        "n_abstained": abstained,
        "n_guessed": len(logged_guessed),
        "n_resolved_guessed": len(guessed_pairs),
        "abstention_rate": _round(abstained / len(scoped), 4) if scoped else None,
        "mean_abs_edge": _round(sum(abs_edges) / len(abs_edges), 4) if abs_edges else None,
        "brier": brier,
        "brier_guessed": brier_guessed,
        "direction_accuracy": (
            _round(direction_correct / len(guessed_pairs), 4) if guessed_pairs else None
        ),
        "bins": bins,
        "mock_split": {
            "live": {"n_logged": live_logged, "n_resolved": live_resolved},
            "mock": {"n_logged": mock_logged, "n_resolved": mock_resolved},
        },
        "label": PAPER_LABEL,
    }


# -------------------------------------------------------------------- pinned --
#: The canned demo snapshot. Chosen so the deterministic mock yields a
#: NON-abstained guess (pinned by ``test_predict_demo``); the demo falls back
#: to this whenever a live upstream is unavailable. It is clearly labelled
#: (``snapshot_source: "canned"``, ``model.mock: true``) and never persisted.
CANNED_DEMO_SNAPSHOT: dict = {
    "market": {
        "question": "Will the demo market resolve YES?",
        "slug": "hunchfall-demo-market",
        "condition_id": "0x" + "0" * 64,
        "yes_token_id": "0",
        "no_token_id": "1",
        "end_date": "2027-12-31T23:59:59Z",
        "hours_to_resolution": 11280.0,
        "volume24hr_usd": 48210.5,
        "closed": False,
    },
    "features": {
        "market_mid": 0.55,
        "best_bid": 0.54,
        "best_ask": 0.56,
        "spread_cents": 2.0,
        "bid_depth_usd": 5210.5,
        "ask_depth_usd": 3871.2,
        "imbalance": 0.1474,
        "tape": {
            "count": 100,
            "buy_usd": 18400.0,
            "sell_usd": 11700.0,
            "flow_imbalance": 0.2227,
            "vwap": 0.5512,
            "momentum_15m": 0.004,
            "momentum_60m": 0.015,
            "volume_accel": 1.8,
        },
        "volume24hr_usd": 48210.5,
        "hours_to_resolution": 11280.0,
        "social": {
            "relevance": "direct",
            "proxy": False,
            "platforms_ok": ["bluesky"],
            "missing": ["reddit", "rss"],
            "posts_window": 41,
            "engagement_window": 12,
            "velocity": 1.4,
            "chatter_score": 0.77,
            "top_item": {
                "platform": "bluesky",
                "text": "canned demo item",
                "url": "",
                "ts": "",
            },
        },
    },
    "snapshot_ts": "2026-09-30T16:40:00Z",
}


class PredictService:
    """Runs the guesser pipeline and owns the prediction ledger reads."""

    def __init__(self, settings: Settings, audit: AuditLog) -> None:
        """Store settings, the audit log, and the social analyzer.

        Args:
            settings: App Settings.
            audit: Append-only audit log.
        """
        self.settings = settings
        self.audit = audit
        self.social = SocialAnalyzer(settings)

    # ---- market resolution -------------------------------------------------
    def resolve_market(
        self,
        gamma: GammaClient,
        *,
        market_slug: str | None = None,
        condition_id: str | None = None,
    ) -> dict:
        """Resolve an identifier to a tradeable market (single seam).

        The slug path is verified. The ``condition_id`` path is live-checked
        (2026-10-01): a raw condition id is rejected by ``/markets/{id}``
        ("id is invalid"), so it resolves through Gamma's verified
        ``/markets?condition_ids=`` filter instead. Failures map to
        404/409/502.

        Args:
            gamma: Gamma client.
            market_slug: Market slug, or None.
            condition_id: Condition id, or None.

        Returns:
            ``{"market", "tokens", "condition_id", "slug"}``.

        Raises:
            PredictNotFound / PredictNotTradeable / PredictUpstreamError.
        """
        try:
            if market_slug:
                market = gamma.get_market_by_slug(market_slug)
            else:
                market = gamma.get_market_by_condition_id(str(condition_id))
        except GammaError as exc:
            if "404" in str(exc):
                raise PredictNotFound(
                    f"Gamma has no market for {market_slug or condition_id!r}"
                ) from exc
            self.audit.record("stage_error", {"stage": "gamma.resolve", "error": str(exc)})
            raise PredictUpstreamError(f"Gamma lookup failed: {exc}", source="gamma") from exc

        if not isinstance(market, dict) or not market.get("question"):
            raise PredictNotFound(
                f"Gamma has no market for {market_slug or condition_id!r}"
            )
        if market.get("closed") or is_resolved(market):
            raise PredictNotTradeable("market is closed/resolved")
        tokens = parse_token_ids(market)
        if len(tokens) < 2:
            raise PredictNotTradeable("market has no two-sided token pair")
        resolved_condition = str(
            market.get("conditionId")
            or market.get("condition_id")
            or market.get("id")
            or ""
        )
        if not resolved_condition:
            raise PredictNotTradeable("market has no condition id")
        return {
            "market": market,
            "tokens": tokens,
            "condition_id": resolved_condition,
            "slug": str(market.get("slug") or market_slug or ""),
        }

    # ---- snapshot ----------------------------------------------------------
    def build_features(
        self,
        market: dict,
        condition_id: str,
        *,
        clob: ClobClient,
        data_api: DataApiClient,
    ) -> tuple[dict, list[str]]:
        """Assemble the deterministic feature block + the missing-source list.

        The CLOB book is required (raises); the tape and the social pulse are
        features and simply go missing.

        Args:
            market: Gamma market dict.
            condition_id: Resolved condition id.
            clob: CLOB client.
            data_api: Data API client.

        Returns:
            ``(features, missing)``.
        """
        levels = int(getattr(self.settings, "PREDICT_BOOK_LEVELS", 5) or 5)
        tokens = parse_token_ids(market)
        try:
            book = clob.get_orderbook(tokens[0]) or {}
        except Exception as exc:  # noqa: BLE001 - required upstream, mapped to 502
            self.audit.record("stage_error", {"stage": "clob.book", "error": str(exc)})
            raise PredictUpstreamError(f"CLOB book failed: {exc}", source="clob") from exc

        try:
            features = book_features(book, levels)
        except PredictUpstreamError as exc:
            self.audit.record(
                "stage_error", {"stage": "clob.book", "error": exc.message}
            )
            raise
        missing: list[str] = []
        if features.get("one_sided"):
            # The book cannot provide a mid when one half is empty; resolve it
            # through the documented fallback chain and name the missing side.
            # This is a data-quality state, not an upstream failure.
            features["market_mid"], mid_source = self._resolve_one_sided_mid(
                book, tokens[0], clob
            )
            features["mid_source"] = mid_source
            missing.append(f"book_{features['one_sided']}")

        limit = int(getattr(self.settings, "PREDICT_TAPE_LIMIT", 100) or 100)
        # Prefer filter_type=CASH (documented USD-denominated rows); fall back
        # to the plain TOKENS shape if CASH is rejected. **Live-checked
        # 2026-10-02: the condition shape accepts CASH but does not honor it**
        # — CASH and TOKENS rows came back byte-identical, `size` still in
        # shares — so USD is and stays `size × price`, and the basis actually
        # used is recorded with the features instead of implied.
        rows: list[dict] | None = None
        try:
            rows = data_api.get_trades_v2(
                condition=condition_id, limit=limit, filter_type="CASH"
            )
        except DataApiError:
            rows = None  # CASH rejected: fall through to the plain shape
        if rows is None:
            try:
                rows = data_api.get_trades_v2(condition=condition_id, limit=limit)
            except DataApiError as exc:
                self.audit.record(
                    "stage_error", {"stage": "data-api.trades", "error": str(exc)}
                )
                missing.append("tape")
        if rows is not None:
            # /v2/trades interleaves both outcomes; the tape is the YES token's
            # flow only (live-checked 2026-10-01 — see API_INVENTORY.md).
            features["tape"] = tape_features(
                rows or [], yes_token_id=tokens[0] if tokens else None
            )
            features["tape"]["usd_basis"] = "size_x_price"

        try:
            history = clob.get_prices_history(tokens[0], interval="1h") or []
            history_features = price_history_features(history)
            if history_features:
                features["price_history"] = history_features
            else:
                missing.append("price_history")
        except Exception as exc:  # noqa: BLE001 - a feature source, never fatal
            self.audit.record(
                "stage_error", {"stage": "clob.prices-history", "error": str(exc)}
            )
            missing.append("price_history")

        volume = volume24hr_usd(market)
        if volume is not None:
            features["volume24hr_usd"] = _round(volume, 2)
        hours = hours_to_resolution(market)
        if hours is not None:
            features["hours_to_resolution"] = _round(hours, 4)

        social = self.social.analyze(market)
        features["social"] = social
        return features, missing

    def _resolve_one_sided_mid(
        self, book: dict, token_id: str, clob: ClobClient
    ) -> tuple[float, str]:
        """Mid for a one-sided book: ``/midpoint``, then ``last_trade_price``.

        The book cannot provide a mid when one half is empty; the market's
        own ``/midpoint`` can. When even that fails, the last traded price is
        the only defensible anchor left, and if neither exists the prediction
        fails closed (502) — never guessed.

        Args:
            book: CLOB ``/book`` payload.
            token_id: YES token id.
            clob: CLOB client.

        Returns:
            ``(mid, source)`` with source in {"midpoint", "last_trade"}.

        Raises:
            PredictUpstreamError: no defensible mid exists (fail closed).
        """
        try:
            mid = float(clob.get_midpoint(token_id))
        except Exception as exc:  # noqa: BLE001 - fall through to last trade
            self.audit.record(
                "stage_error", {"stage": "clob.midpoint", "error": str(exc)}
            )
            last = _float(book.get("last_trade_price"))
            if last is not None and 0.0 < last < 1.0:
                return _round(last, 6), "last_trade"
            raise PredictUpstreamError(
                "one-sided book and no /midpoint or last_trade_price available",
                source="clob",
            ) from exc
        return _round(mid, 6), "midpoint"

    def _snapshot_digest(self, question: str, condition_id: str, features: dict) -> str:
        """Stable sha256 over the canonical snapshot (features + identity)."""
        canonical = json.dumps(
            {"question": question, "condition_id": condition_id, "features": features},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ---- model -------------------------------------------------------------
    def _ensemble(self, jev: JevClient, state: DecisionState) -> dict:
        """Ask the model N times; return the median P(YES) + disagreement.

        Args:
            jev: Jev client.
            state: Compact decision state.

        Returns:
            ``{"p_yes", "disagreement", "decision", "ensemble_n"}``.

        Raises:
            PredictUpstreamError: when the model call fails (nothing persisted).
        """
        n = int(getattr(self.settings, "PREDICT_ENSEMBLE_N", 3) or 3)
        n = max(1, min(5, n))
        decisions = []
        for _ in range(n):
            try:
                decisions.append(jev.decide(state))
            except Exception as exc:  # noqa: BLE001 - mapped to 502
                self.audit.record("stage_error", {"stage": "jev.decide", "error": str(exc)})
                raise PredictUpstreamError(
                    f"Jev decision failed: {exc}", source="jev"
                ) from exc
        values = [float(d.p_true) for d in decisions]
        return {
            "p_yes": _round(statistics.median(values), 4),
            "disagreement": _round(max(values) - min(values), 4),
            "decision": decisions[0],
            "ensemble_n": n,
        }

    def _model_meta(self, decision: Any, ensemble: dict) -> dict:
        """The labelled model block (mock is always explicit)."""
        mock = bool(getattr(decision, "mock", False))
        return {
            "mode": "mock" if mock else "live",
            "mock": mock,
            "version": getattr(decision, "jev_model", "") or MOCK_VERSION,
            "ensemble_n": ensemble["ensemble_n"],
            "disagreement": ensemble["disagreement"],
            "note": MOCK_NOTE if mock else "",
        }

    def _threshold(self) -> float:
        """``PREDICT_ABSTAIN_EDGE`` — read without ``or`` so 0.0 survives.

        A deliberate ``0.0`` means "never abstain"; coercing it back to the
        default would silently override the operator. Same None-check pattern
        as ``SOCIAL_DEADLINE_SEC`` in ``app/social.py``.

        Returns:
            The abstention threshold (0.10 when the setting is unset).
        """
        raw = getattr(self.settings, "PREDICT_ABSTAIN_EDGE", 0.10)
        return 0.10 if raw is None else float(raw)

    def _make_client(self, factory: Callable[[Settings], Any], source: str) -> Any:
        """Construct an upstream client, mapping failures to a 502 envelope.

        Nothing is persisted before the ``predict`` event, so a constructor
        failure is already fail-closed; this only keeps the error envelope
        uniform instead of leaking an unhandled 500.

        Args:
            factory: A client class taking ``Settings``.
            source: Envelope source name (gamma | clob | data-api | jev).

        Returns:
            The constructed client.

        Raises:
            PredictUpstreamError: the client could not be constructed.
        """
        try:
            return factory(self.settings)
        except Exception as exc:  # noqa: BLE001 - uniform 502 envelope
            raise PredictUpstreamError(
                f"{source} client unavailable: {exc}", source=source
            ) from exc

    # ---- main entry --------------------------------------------------------
    def run(self, req: dict[str, Any]) -> dict:
        """Execute one prediction and persist a ``predict`` event.

        Args:
            req: Validated ``PredictRequest`` fields as a plain dict.

        Returns:
            The ``POST /predict`` response body.

        Raises:
            PredictError: every failure maps to an HTTP status + code.
        """
        market_slug = str(req.get("market_slug") or "").strip() or None
        condition_id = str(req.get("condition_id") or "").strip() or None

        gamma = clob = data_api = jev = None
        try:
            gamma = self._make_client(GammaClient, "gamma")
            resolved = self.resolve_market(
                gamma, market_slug=market_slug, condition_id=condition_id
            )
            market = resolved["market"]
            question = str(market.get("question") or resolved["slug"])

            clob = self._make_client(ClobClient, "clob")
            data_api = self._make_client(DataApiClient, "data-api")
            features, missing = self.build_features(
                market, resolved["condition_id"], clob=clob, data_api=data_api
            )

            snapshot_ts = utcnow_iso()
            snapshot_text = build_snapshot_text(
                question, features["market_mid"], features, missing
            )
            jev = self._make_client(JevClient, "jev")
            ensemble = self._ensemble(
                jev,
                DecisionState(
                    news_summary=(features.get("social") or {}).get("top_item", {}).get(
                        "text"
                    )
                    or "no fresh social data",
                    market_question=question,
                    yes_price=features["market_mid"],
                    extra_context=snapshot_text,
                ),
            )
            threshold = self._threshold()
            decision = decide(
                ensemble["p_yes"],
                features["market_mid"],
                threshold,
                ensemble["disagreement"],
            )
            decision = _force_data_quality_abstention(features, decision)
            reasons = build_reasons(features, decision, missing, threshold)
            model_meta = self._model_meta(ensemble["decision"], ensemble)
            digest = self._snapshot_digest(question, resolved["condition_id"], features)

            prediction_id = str(uuid4())
            event_id = self.audit.record(
                "predict",
                {
                    "prediction_id": prediction_id,
                    "condition_id": resolved["condition_id"],
                    "slug": resolved["slug"],
                    "question": question,
                    "p_yes": ensemble["p_yes"],
                    "market_price": features["market_mid"],
                    "edge_vs_market": decision["edge_vs_market"],
                    "direction": decision["direction"],
                    "confidence": decision["confidence"],
                    "abstained": decision["abstained"],
                    "abstain_threshold": threshold,
                    "model": model_meta,
                    "snapshot_sha256": digest,
                    "snapshot_ts": snapshot_ts,
                    "features": features,
                    "missing": missing,
                    "social": {
                        "relevance": (features.get("social") or {}).get("relevance"),
                        "proxy": (features.get("social") or {}).get("proxy"),
                        "platforms_ok": (features.get("social") or {}).get("platforms_ok", []),
                        "missing": (features.get("social") or {}).get("missing", []),
                    },
                    "reasons": reasons,
                },
            )
            return self._prediction_response(
                prediction_id=prediction_id,
                event_id=event_id,
                market=market,
                resolved=resolved,
                features=features,
                decision=decision,
                p_yes=ensemble["p_yes"],
                threshold=threshold,
                reasons=reasons,
                model_meta=model_meta,
                digest=digest,
                snapshot_ts=snapshot_ts,
                missing=missing,
            )
        finally:
            for client in (gamma, clob, data_api, jev):
                if client is not None:
                    try:
                        client.close()
                    except Exception:  # noqa: BLE001 - best effort
                        pass

    def _prediction_response(
        self,
        *,
        prediction_id: str,
        event_id: int | None,
        market: dict,
        resolved: dict,
        features: dict,
        decision: dict,
        p_yes: float,
        threshold: float,
        reasons: list[str],
        model_meta: dict,
        digest: str,
        snapshot_ts: str,
        missing: list[str],
    ) -> dict:
        """Assemble the shared prediction envelope (§7.1 / §7.2)."""
        return {
            "mode": "PAPER",
            "market": {
                "question": str(market.get("question") or resolved.get("slug") or ""),
                "slug": resolved.get("slug") or "",
                "condition_id": resolved.get("condition_id") or "",
                "yes_token_id": (resolved.get("tokens") or ["", ""])[0],
                "no_token_id": (resolved.get("tokens") or ["", ""])[1],
                "end_date": str(market.get("endDate") or market.get("end_date") or ""),
                "hours_to_resolution": features.get("hours_to_resolution"),
                "volume24hr_usd": features.get("volume24hr_usd"),
                "closed": bool(market.get("closed")),
            },
            "prediction": {
                "prediction_id": prediction_id,
                "p_yes": p_yes,
                "direction": decision["direction"],
                "confidence": decision["confidence"],
                "edge_vs_market": decision["edge_vs_market"],
                "abstained": decision["abstained"],
                "reasons": reasons,
                "snapshot_ts": snapshot_ts,
            },
            "market_price": features.get("market_mid"),
            "abstain_threshold": threshold,
            "model": model_meta,
            "snapshot": {"sha256": digest, "missing": missing, "features": features},
            "audit_event_id": event_id,
            "label": PAPER_LABEL,
            "trade_placed": False,
        }

    # ---- demo --------------------------------------------------------------
    def demo(self) -> dict:
        """Run the pinned demo market; fall back to the canned snapshot.

        Always 200 and **never persists**.

        Returns:
            The ``GET /predict/demo`` body.
        """
        slug = str(getattr(self.settings, "PREDICT_DEMO_SLUG", "") or "").strip()
        if bool(getattr(self.settings, "PREDICT_DEMO_LIVE", True)) and slug:
            try:
                return self._demo_live(slug)
            except Exception as exc:  # noqa: BLE001 - canned fallback by design
                self.audit.record(
                    "stage_error", {"stage": "predict.demo.live", "error": str(exc)}
                )
        return self._demo_canned()

    def _demo_live(self, slug: str) -> dict:
        """Live demo path: full pipeline, no ledger write."""
        gamma = clob = data_api = jev = None
        try:
            gamma = GammaClient(self.settings)
            resolved = self.resolve_market(gamma, market_slug=slug)
            market = resolved["market"]
            clob = ClobClient(self.settings)
            data_api = DataApiClient(self.settings)
            features, missing = self.build_features(
                market, resolved["condition_id"], clob=clob, data_api=data_api
            )
            jev = JevClient(self.settings)
            ensemble = self._ensemble(
                jev,
                DecisionState(
                    news_summary="predict demo",
                    market_question=str(market.get("question") or slug),
                    yes_price=features["market_mid"],
                    extra_context=build_snapshot_text(
                        str(market.get("question") or slug),
                        features["market_mid"],
                        features,
                        missing,
                    ),
                ),
            )
            threshold = self._threshold()
            decision = decide(
                ensemble["p_yes"],
                features["market_mid"],
                threshold,
                ensemble["disagreement"],
            )
            decision = _force_data_quality_abstention(features, decision)
            reasons = build_reasons(features, decision, missing, threshold)
            body = self._prediction_response(
                prediction_id="demo",
                event_id=None,
                market=market,
                resolved=resolved,
                features=features,
                decision=decision,
                p_yes=ensemble["p_yes"],
                threshold=threshold,
                reasons=reasons,
                model_meta=self._model_meta(ensemble["decision"], ensemble),
                digest=self._snapshot_digest(
                    str(market.get("question") or slug), resolved["condition_id"], features
                ),
                snapshot_ts=utcnow_iso(),
                missing=missing,
            )
            body["demo"] = True
            body["snapshot_source"] = "live"
            return body
        finally:
            for client in (gamma, clob, data_api, jev):
                if client is not None:
                    try:
                        client.close()
                    except Exception:  # noqa: BLE001 - best effort
                        pass

    @staticmethod
    def _demo_canned() -> dict:
        """Canned demo snapshot — pinned, deterministic, never persisted."""
        canned = CANNED_DEMO_SNAPSHOT
        features = {key: value for key, value in canned["features"].items()}
        market = dict(canned["market"])
        # The pinned p_yes is chosen so the demo always shows a real result
        # card (non-abstained); pinned by test_predict_demo.
        p_yes = 0.67
        threshold = 0.10
        decision = decide(p_yes, features["market_mid"], threshold, 0.0)
        reasons = build_reasons(features, decision, [], threshold)
        return {
            "mode": "PAPER",
            "demo": True,
            "snapshot_source": "canned",
            "market": market,
            "prediction": {
                "prediction_id": "demo",
                "p_yes": p_yes,
                "direction": decision["direction"],
                "confidence": decision["confidence"],
                "edge_vs_market": decision["edge_vs_market"],
                "abstained": decision["abstained"],
                "reasons": reasons,
                "snapshot_ts": canned["snapshot_ts"],
            },
            "market_price": features["market_mid"],
            "abstain_threshold": threshold,
            "model": {
                "mode": "mock",
                "mock": True,
                "version": MOCK_VERSION,
                "ensemble_n": 1,
                "disagreement": 0.0,
                "note": MOCK_NOTE,
            },
            "snapshot": {"sha256": "canned", "missing": [], "features": features},
            "audit_event_id": None,
            "label": PAPER_LABEL,
            "trade_placed": False,
        }

    # ---- ledger ------------------------------------------------------------
    def accuracy(self, *, include_mock: bool = False, limit: int = 500) -> dict:
        """``GET /predict/accuracy`` — derived-on-read ledger.

        Args:
            include_mock: Count mock predictions (default False).
            limit: Max predictions scanned.

        Returns:
            Response body.
        """
        report = accuracy_report(
            self.audit.predictions(limit=limit),
            self.audit.prediction_resolutions(limit=limit),
            include_mock=include_mock,
            limit=limit,
        )
        return {
            "mode": "PAPER",
            "as_of": utcnow_iso(),
            "include_mock": include_mock,
            **report,
        }

    def resolve_prediction(self, prediction_id: str, outcome: str) -> dict:
        """Record the realised outcome for one logged prediction.

        Args:
            prediction_id: The ``predict`` event's uuid.
            outcome: ``YES`` | ``NO``.

        Returns:
            Response body.

        Raises:
            PredictNotFound: unknown prediction id (404).
            PredictError(409): already resolved.
        """
        event = self.audit.prediction_by_id(prediction_id)
        if event is None:
            exc = PredictNotFound(f"no prediction matches {prediction_id!r}")
            exc.code = "unknown_prediction"
            raise exc
        previous = self.audit.prediction_resolution(prediction_id)
        if previous is not None:
            exc = PredictError(
                f"prediction {prediction_id} was already resolved as "
                f"{(previous.get('payload') or {}).get('outcome')}"
            )
            exc.status = 409
            exc.code = "already_resolved"
            raise exc

        payload = event.get("payload") or {}
        resolved_at = utcnow_iso()
        self.audit.record(
            "predict_resolved",
            {
                "prediction_id": prediction_id,
                "outcome": outcome,
                "resolved_at": resolved_at,
                "source": "manual",
            },
        )
        outcome_value = 1.0 if outcome == "YES" else 0.0
        p_yes = _float(payload.get("p_yes")) or 0.0
        market = _float(payload.get("market_price")) or 0.0
        model_brier = (p_yes - outcome_value) ** 2
        market_brier = (market - outcome_value) ** 2
        return {
            "mode": "PAPER",
            "prediction_id": prediction_id,
            "outcome": outcome,
            "resolved_at": resolved_at,
            "prediction": {
                "p_yes": payload.get("p_yes"),
                "direction": payload.get("direction"),
                "confidence": payload.get("confidence"),
                "abstained": payload.get("abstained"),
            },
            "brier": {
                "model": _round(model_brier, 4),
                "market": _round(market_brier, 4),
                "baseline_0_5": 0.25,
                "skill_vs_market": (
                    _round(1.0 - model_brier / market_brier, 4)
                    if market_brier > 0
                    else None
                ),
                "skill_vs_baseline": _round(1.0 - model_brier / 0.25, 4),
            },
            "label": PAPER_LABEL,
        }
