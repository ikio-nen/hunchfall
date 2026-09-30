"""Paper execution engine: simulated fills against live CLOB touch prices.

PAPER TRADING ONLY — no orders are placed, no wallet is touched. A fill is
modeled as a taker crossing the spread:

* ``token_price`` = YES mid price, or ``1 - YES mid`` when buying NO.
* ``intended_price`` (touch) = ``token_price + half_spread`` — the taker
  pays half the spread on a marketable order (modeled).
* **Book-walk impact**: when the requested notional exceeds 1% of the
  visible take-side depth, the engine walks the book level-by-level
  (``Signal.book_asks`` for a YES buy; mirrored ``book_bids`` for a NO
  buy), accumulating price x shares per level. The volume-weighted
  average becomes the simulated fill price.
* **Partial fills**: when the visible depth is smaller than the requested
  size, the engine fills only what is available; ``contracts``,
  ``filled_usd`` and the note record this honestly.
* **Taker fee** (verified formula): ``fee = C x feeRate x p x (1-p)``
  where C = filled shares, p = fill price, and the category rate comes
  from ``FEE_RATES`` (or ``fee_rate_override`` when the per-market payload
  is authoritative). Makers pay 0 — we always take.
* **Slippage**: ``(vwap_fill_price - token_price) x contracts`` — half-
  spread crossing plus book-walk impact, both modeled. ``Fill.price`` is
  the simulated (vwap) fill price; ``Fill.intended_price`` is the touch.

Every fill is recorded to the audit log via ``audit.record("fill", ...)``.
Nothing here invents prices: the touch comes from the live CLOB snapshot
on the Signal, and every modeled cost is labeled as such in the fill note.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4

from app.policy.gate import Signal, buy_direction

#: Verified taker fee rates by market category (fraction). Makers pay 0;
#: HUNCHFALL always takes, so the taker rate applies. Unknown categories
#: fall back to "Other" (0.05).
FEE_RATES: dict[str, float] = {
    "Crypto": 0.07,
    "Sports": 0.05,
    "Economics": 0.05,
    "Culture": 0.05,
    "Weather": 0.05,
    "Other": 0.05,
    "Finance": 0.04,
    "Politics": 0.04,
    "Mentions": 0.04,
    "Tech": 0.04,
    "Geopolitics": 0.0,
}

#: Walk the book when the requested notional exceeds this share of the
#: visible take-side depth.
BOOK_WALK_DEPTH_FRAC = 0.01


def fee_rate_for(category: str | None) -> float:
    """Taker fee rate for a market category; "Other" (0.05) when unknown.

    Args:
        category: Category label, e.g. "Politics".

    Returns:
        Fee rate fraction.
    """
    if not category:
        return FEE_RATES["Other"]
    for name, rate in FEE_RATES.items():
        if name.lower() == str(category).strip().lower():
            return rate
    return FEE_RATES["Other"]


def taker_fee_usd(contracts: float, price: float, rate: float) -> float:
    """Verified taker-fee formula: ``fee = C x feeRate x p x (1-p)``.

    Args:
        contracts: Filled shares (C).
        price: Fill price (p).
        rate: Category taker fee rate.

    Returns:
        Fee in USD.
    """
    return contracts * rate * price * (1.0 - price)


@dataclass
class Fill:
    """One simulated fill."""

    fill_id: str
    market_id: str
    token_id: str
    side: str  # "YES" or "NO" — the side bought
    price: float  # volume-weighted average SIMULATED fill price of the token
    intended_price: float  # touch price before book-walk (modeled)
    size_usd: float  # requested notional
    filled_usd: float  # actually filled notional (= contracts x price)
    contracts: float  # filled shares
    fee_usd: float  # modeled taker fee: C x rate x p x (1-p)
    slippage_usd: float  # modeled: (fill price - token price) x contracts
    ts: str  # ISO-8601 UTC
    note: str  # documents the fill model used (touch vs book-walk/partial)


@dataclass
class Position:
    """One paper position, token-denominated."""

    market_id: str
    token_id: str
    side: str  # "YES" or "NO"
    contracts: float
    avg_price: float  # volume-weighted entry price of the token
    current_price: float  # latest mark price of the token
    unrealized_pnl: float  # (current_price - avg_price) * contracts


class PaperEngine:
    """Simulates taker fills; records each one to the audit log."""

    def __init__(self, settings, audit) -> None:
        """Store settings and the audit log.

        Args:
            settings: App Settings (FEE_RATE_OVERRIDE, ...).
            audit: AuditLog-like object with ``record(event_type, payload)``.
        """
        self._s = settings
        self._audit = audit

    @staticmethod
    def _take_levels(signal: Signal, side: str) -> list[tuple[float, float]]:
        """Take-side book levels as (price, size) for the side we buy.

        Args:
            signal: Approved signal carrying the YES-token book levels.
            side: "YES" (take asks) or "NO" (take mirrored bids).

        Returns:
            List of (price, size_shares) best-first.
        """
        if side == "YES":
            raw = signal.book_asks or []
            levels = []
            for lvl in raw:
                try:
                    levels.append((float(lvl[0]), float(lvl[1])))
                except (TypeError, ValueError, IndexError):
                    continue
            return levels
        # NO token asks = mirrored YES bids, best-first.
        raw = signal.book_bids or []
        levels = []
        for lvl in reversed(raw):
            try:
                levels.append((1.0 - float(lvl[0]), float(lvl[1])))
            except (TypeError, ValueError, IndexError):
                continue
        return levels

    def execute(
        self,
        signal: Signal,
        size_usd: float,
        fee_category: str = "Other",
        fee_rate_override: float | None = None,
    ) -> Fill:
        """Simulate a taker fill for an approved signal.

        Args:
            signal: Approved signal (carries the live CLOB touch inputs and
                book levels).
            size_usd: Approved notional from the risk gate.
            fee_category: Market category for the taker-fee rate
                (Crypto 0.07, Sports/Economics/Culture/Weather/Other 0.05,
                Finance/Politics/Mentions/Tech 0.04, Geopolitics 0.0).
            fee_rate_override: When the per-market payload carries an
                authoritative fee rate, it wins over the category table.

        Returns:
            Fill recorded to the audit log.
        """
        side = buy_direction(signal)
        token_price = (
            signal.market_price if side == "YES" else 1.0 - signal.market_price
        )
        half_spread = (signal.spread_cents / 100.0) / 2.0
        intended = token_price + half_spread  # touch: taker crosses half-spread

        levels = self._take_levels(signal, side)
        visible_depth_usd = sum(p * s for p, s in levels)

        if intended <= 0 or size_usd <= 0:
            vwap, contracts, note = 0.0, 0.0, "no fill: degenerate price/size"
        elif not levels or size_usd <= BOOK_WALK_DEPTH_FRAC * visible_depth_usd:
            # Small relative to the book: fill at the touch.
            vwap = intended
            contracts = size_usd / intended if intended > 0 else 0.0
            note = "full fill at touch (taker crosses half-spread; modeled)"
        else:
            # Walk the book level-by-level.
            shares_needed = size_usd / intended
            filled_cost = 0.0
            filled_shares = 0.0
            for price, size in levels:
                take = min(shares_needed - filled_shares, size)
                if take <= 0:
                    break
                filled_cost += take * price
                filled_shares += take
            if filled_shares <= 0:
                vwap, contracts = intended, 0.0
                note = "no fill: empty take-side book (modeled)"
            else:
                vwap = filled_cost / filled_shares
                contracts = filled_shares
                if filled_shares < shares_needed:
                    note = (
                        f"PARTIAL fill: visible depth ${visible_depth_usd:.2f} "
                        f"< requested ${size_usd:.2f}; filled {filled_shares:.1f} "
                        f"shares at vwap {vwap:.4f} (modeled)"
                    )
                else:
                    note = (
                        f"book-walk fill: size ${size_usd:.2f} > 1% of visible "
                        f"depth ${visible_depth_usd:.2f}; vwap {vwap:.4f} vs "
                        f"touch {intended:.4f} (modeled)"
                    )

        rate = (
            fee_rate_override
            if fee_rate_override is not None
            else fee_rate_for(fee_category)
        )
        fee_usd = taker_fee_usd(contracts, vwap, rate)
        # Slippage = half-spread crossing + book-walk impact, both modeled.
        slippage_usd = max(0.0, (vwap - token_price)) * contracts
        filled_usd = contracts * vwap

        fill = Fill(
            fill_id=uuid4().hex[:12],
            market_id=signal.market_id,
            token_id=signal.token_id,
            side=side,
            price=vwap,
            intended_price=intended,
            size_usd=size_usd,
            filled_usd=filled_usd,
            contracts=contracts,
            fee_usd=fee_usd,
            slippage_usd=slippage_usd,
            ts=datetime.now(timezone.utc).isoformat(),
            note=note,
        )
        self._audit.record("fill", asdict(fill))
        return fill


def mark_to_market(
    positions: list[Position],
    price_fn: Callable[[str, str, str], float],
) -> list[Position]:
    """Re-mark positions to current token prices.

    Args:
        positions: Paper positions to re-mark.
        price_fn: ``(market_id, token_id, side) -> float`` returning the
            current price of that side's token (for NO positions this must
            be the NO-token price, i.e. ``1 - yes_price``).

    Returns:
        New Position list with ``current_price`` and ``unrealized_pnl``
        refreshed. PnL is token-denominated:
        ``(current_price - avg_price) * contracts``.
    """
    marked: list[Position] = []
    for p in positions:
        current = price_fn(p.market_id, p.token_id, p.side)
        marked.append(
            Position(
                market_id=p.market_id,
                token_id=p.token_id,
                side=p.side,
                contracts=p.contracts,
                avg_price=p.avg_price,
                current_price=current,
                unrealized_pnl=(current - p.avg_price) * p.contracts,
            )
        )
    return marked
