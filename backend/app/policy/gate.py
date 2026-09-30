"""Deterministic risk gate for hunchfall.

The model (Jev) only supplies ``P(true)`` plus a typed choice and a
confidence. This module owns the decision to act: whether the
fee-adjusted edge is big enough, which side to buy, and how much to risk.
Every rejection is returned as an explicit :class:`Veto` so the veto log
is complete and honest. All thresholds come from ``Settings`` (env) —
nothing is hardcoded here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps runtime decoupled
    from app.config import Settings


@dataclass
class Signal:
    """One evaluated opportunity."""

    market_id: str
    token_id: str
    question: str
    p_true: float  # Jev P(true) (raw noul), in [0, 1]
    market_price: float  # live CLOB mid price of the YES token, in (0, 1)
    spread_cents: float  # bid-ask spread in USD cents (veto if > 3.0)
    top_book_depth_usd: float  # resting USD at the best bid+ask (veto if < 50)
    hours_to_resolution: float
    # ISO-8601 timestamps; veto if the news snapshot is older than the
    # price snapshot by more than MAX_NEWS_PRICE_SKEW_SEC.
    news_ts: str = ""
    price_ts: str = ""
    # Sum of parsed outcomePrices (YES+NO should total ~1); veto outside
    # [NEGRISK_SUM_MIN, NEGRISK_SUM_MAX] or when unparseable.
    negrisk_sum: float | None = None
    # UMA dispute flag from the market payload; veto when True.
    uma_dispute: bool = False
    # Jev typed-call outputs; veto when confidence < JEV_CONFIDENCE_MIN
    # or the model abstained (choice == "SKIP" -> veto "model_skip").
    jev_confidence: float = 0.0
    jev_choice: str = ""
    # Fee category for the taker-fee estimate (Crypto/Sports/.../Other).
    fee_category: str = "Other"
    # Top-N book levels of the YES token for the execution engine's
    # book-walk: [[price, size_shares], ...]. The engine takes asks for a
    # YES buy, or mirrored bids for a NO buy.
    book_asks: list = field(default_factory=list)
    book_bids: list = field(default_factory=list)


@dataclass
class Veto:
    """One reason a signal was rejected."""

    reason: str  # machine-readable code, e.g. "edge_too_small"
    detail: str  # human-readable explanation with the numbers


@dataclass
class GateResult:
    """Outcome of :meth:`RiskGate.evaluate`."""

    approved: bool
    size_usd: float  # approved notional; 0.0 when vetoed
    edge: float  # raw abs(p_true - market_price)
    fee_adjusted_edge: float  # edge minus estimated taker fee per dollar
    vetoes: list[Veto] = field(default_factory=list)


def buy_direction(signal: Signal) -> str:
    """Return the side to buy: ``"YES"`` when our P(true) exceeds the market
    price, otherwise ``"NO"``."""
    return "YES" if signal.p_true > signal.market_price else "NO"


def kelly_fraction(p_side: float, price_side: float, fraction: float) -> float:
    """Fractional-Kelly stake for one side of a binary contract.

    A binary contract costs ``price_side`` and pays 1.0 on a win, so the net
    odds are ``b = (1 - price_side) / price_side``. With win probability
    ``p_side`` (``q = 1 - p_side``), the full Kelly fraction is::

        f* = (p_side * b - q) / b  =  (p_side - price_side) / (1 - price_side)

    The second form shows the intuition directly: the stake grows with the
    gap between our probability and the market price, scaled by the payout
    multiple. (Derivation: maximize ``p*log(1+b*f) + q*log(1-f)`` in ``f``;
    the optimum is ``f* = (p*b - q)/b``.)

    Args:
        p_side: Our win probability for the side we buy, in [0, 1].
        price_side: Market price of that side's token, in (0, 1).
        fraction: Kelly fraction multiplier (e.g. 0.25 = quarter-Kelly).

    Returns:
        ``fraction * f*`` floored at 0 (never size a negative edge), and 0.0
        for degenerate prices outside (0, 1).
    """
    if not 0.0 < price_side < 1.0:
        return 0.0
    if not 0.0 <= p_side <= 1.0:
        return 0.0
    b = (1.0 - price_side) / price_side
    q = 1.0 - p_side
    f_star = (p_side * b - q) / b
    return max(0.0, fraction * f_star)


def _parse_ts(raw: str) -> datetime | None:
    """Parse an ISO-8601 timestamp; None when missing/unparseable."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class RiskGate:
    """Deterministic policy layer: approve/veto signals and size positions."""

    def __init__(self, settings: "Settings") -> None:
        """Store settings; all thresholds are read from it at evaluate time.

        Args:
            settings: App Settings (EDGE_THRESHOLD, KELLY_FRACTION, ...).
        """
        self._s = settings

    def evaluate(
        self,
        signal: Signal,
        current_exposure_usd: float,
        bankroll_usd: float,
        open_positions_count: int = 0,
    ) -> GateResult:
        """Apply every rule and collect *all* vetoes (no short-circuiting, so
        the veto log shows every reason a signal failed).

        Rules:
          (0) sanity: p_true in [0, 1] and market_price in (0, 1), otherwise
              the Kelly math below is undefined -> veto ``bad_inputs``.
          (a) quarter-Kelly sizing on the side we buy, clamped to
              MAX_PER_MARKET_FRAC of bankroll.
          (b) fee-adjusted edge = |p_true - market_price| - estimated taker
              fee per $1 notional (fee = C x rate x p x (1-p), so
              fee/$ = rate x (1-p_side)); veto ``edge_too_small`` when it is
              below EDGE_THRESHOLD.
          (c) veto ``wide_spread`` if spread_cents/100 > SPREAD_VETO_CENTS.
          (d) veto ``thin_book`` if top_book_depth_usd < MIN_TOP_DEPTH_USD.
          (e) veto ``near_resolution`` if hours_to_resolution <
              MIN_HOURS_TO_RESOLUTION.
          (f) veto ``news_price_skew`` if the news snapshot is older than the
              price snapshot by more than MAX_NEWS_PRICE_SKEW_SEC
              (``news_price_skew_unknown`` when a timestamp is missing).
          (g) veto ``negrisk_sum_invalid`` if negrisk_sum is unavailable or
              outside [NEGRISK_SUM_MIN, NEGRISK_SUM_MAX].
          (h) veto ``uma_dispute`` when uma_dispute is True.
          (i) veto ``low_jev_confidence`` if jev_confidence <
              JEV_CONFIDENCE_MIN.
          (j) veto ``model_skip`` if jev_choice == "SKIP".
          (k) veto ``exposure_cap`` if current_exposure + sized notional >
              MAX_TOTAL_EXPOSURE_FRAC * bankroll.
          (l) veto ``max_positions`` if open_positions_count >=
              MAX_CONCURRENT_POSITIONS.

        Args:
            signal: The opportunity to judge.
            current_exposure_usd: Current portfolio exposure (mark-to-market).
            bankroll_usd: Starting paper bankroll.
            open_positions_count: Number of currently open positions.

        Returns:
            GateResult with approved/size/edge/fee-adjusted edge and the
            full veto list.
        """
        # Imported here to keep the gate decoupled from execution at import
        # time (paper.py imports from this module).
        from app.execution.paper import fee_rate_for  # noqa: PLC0415

        s = self._s
        vetoes: list[Veto] = []

        # (0) sanity: keep the Kelly math defined.
        if not (0.0 <= signal.p_true <= 1.0 and 0.0 < signal.market_price < 1.0):
            vetoes.append(
                Veto(
                    "bad_inputs",
                    f"p_true={signal.p_true} or market_price={signal.market_price} "
                    "outside valid range",
                )
            )

        # direction + (a) quarter-Kelly sizing on the side we would buy.
        direction = buy_direction(signal)
        if direction == "YES":
            p_side, price_side = signal.p_true, signal.market_price
        else:
            p_side, price_side = 1.0 - signal.p_true, 1.0 - signal.market_price
        f = kelly_fraction(p_side, price_side, s.KELLY_FRACTION)
        f = min(f, s.MAX_PER_MARKET_FRAC)
        size = f * bankroll_usd

        # (b) fee-adjusted edge. Fee = C x rate x p x (1-p); per $1 notional
        # (C x p_side ~= $1) the fee drag is rate x (1 - p_side).
        rate = fee_rate_for(signal.fee_category)
        fee_per_dollar = rate * (1.0 - price_side)
        raw_edge = abs(signal.p_true - signal.market_price)
        fee_adjusted_edge = raw_edge - fee_per_dollar
        if fee_adjusted_edge < s.EDGE_THRESHOLD:
            vetoes.append(
                Veto(
                    "edge_too_small",
                    f"fee-adjusted edge {fee_adjusted_edge:.4f} "
                    f"(raw {raw_edge:.4f} - fee {fee_per_dollar:.4f}) < "
                    f"EDGE_THRESHOLD {s.EDGE_THRESHOLD}",
                )
            )

        # (c) spread (cents field vs dollar-denominated config).
        if signal.spread_cents / 100.0 > s.SPREAD_VETO_CENTS:
            vetoes.append(
                Veto(
                    "wide_spread",
                    f"spread {signal.spread_cents:.1f}c > SPREAD_VETO_CENTS "
                    f"{s.SPREAD_VETO_CENTS * 100:.0f}c",
                )
            )

        # (d) top-of-book depth.
        if signal.top_book_depth_usd < s.MIN_TOP_DEPTH_USD:
            vetoes.append(
                Veto(
                    "thin_book",
                    f"top-of-book depth ${signal.top_book_depth_usd:.2f} < "
                    f"MIN_TOP_DEPTH_USD ${s.MIN_TOP_DEPTH_USD}",
                )
            )

        # (e) time to resolution.
        if signal.hours_to_resolution < s.MIN_HOURS_TO_RESOLUTION:
            vetoes.append(
                Veto(
                    "near_resolution",
                    f"{signal.hours_to_resolution:.2f}h to resolution < "
                    f"MIN_HOURS_TO_RESOLUTION {s.MIN_HOURS_TO_RESOLUTION}h",
                )
            )

        # (f) news vs price freshness skew.
        news_dt = _parse_ts(signal.news_ts)
        price_dt = _parse_ts(signal.price_ts)
        if news_dt is None or price_dt is None:
            vetoes.append(
                Veto(
                    "news_price_skew_unknown",
                    "cannot verify news-vs-price freshness "
                    f"(news_ts={signal.news_ts!r}, price_ts={signal.price_ts!r}); "
                    "fail-closed",
                )
            )
        else:
            skew_sec = (price_dt - news_dt).total_seconds()
            if skew_sec > s.MAX_NEWS_PRICE_SKEW_SEC:
                vetoes.append(
                    Veto(
                        "news_price_skew",
                        f"news is {skew_sec:.0f}s older than the price snapshot "
                        f"> MAX_NEWS_PRICE_SKEW_SEC {s.MAX_NEWS_PRICE_SKEW_SEC}s",
                    )
                )

        # (g) NegRisk price-sum sanity (YES+NO should total ~1).
        if signal.negrisk_sum is None or not (
            s.NEGRISK_SUM_MIN <= signal.negrisk_sum <= s.NEGRISK_SUM_MAX
        ):
            vetoes.append(
                Veto(
                    "negrisk_sum_invalid",
                    f"negrisk_sum={signal.negrisk_sum} outside "
                    f"[{s.NEGRISK_SUM_MIN}, {s.NEGRISK_SUM_MAX}] — book is "
                    "crossed/broken or feed unparseable",
                )
            )

        # (h) UMA dispute.
        if signal.uma_dispute:
            vetoes.append(
                Veto("uma_dispute", "market is under UMA dispute — no trade")
            )

        # (i) model confidence.
        if signal.jev_confidence < s.JEV_CONFIDENCE_MIN:
            vetoes.append(
                Veto(
                    "low_jev_confidence",
                    f"jev_confidence {signal.jev_confidence:.3f} < "
                    f"JEV_CONFIDENCE_MIN {s.JEV_CONFIDENCE_MIN}",
                )
            )

        # (j) model abstention — the deterministic gate owns the action.
        if signal.jev_choice.upper() == "SKIP":
            vetoes.append(
                Veto("model_skip", "Jev typed choice was SKIP (no directional call)")
            )

        # (k) total exposure cap, using the sized notional.
        cap = s.MAX_TOTAL_EXPOSURE_FRAC * bankroll_usd
        if current_exposure_usd + size > cap:
            vetoes.append(
                Veto(
                    "exposure_cap",
                    f"exposure ${current_exposure_usd:.2f} + size ${size:.2f} > "
                    f"cap ${cap:.2f} "
                    f"({s.MAX_TOTAL_EXPOSURE_FRAC} * bankroll ${bankroll_usd:.2f})",
                )
            )

        # (l) concurrent-position cap.
        if open_positions_count >= s.MAX_CONCURRENT_POSITIONS:
            vetoes.append(
                Veto(
                    "max_positions",
                    f"{open_positions_count} open positions >= "
                    f"MAX_CONCURRENT_POSITIONS {s.MAX_CONCURRENT_POSITIONS}",
                )
            )

        approved = not vetoes
        return GateResult(
            approved=approved,
            size_usd=size if approved else 0.0,
            edge=raw_edge,
            fee_adjusted_edge=fee_adjusted_edge,
            vetoes=vetoes,
        )
