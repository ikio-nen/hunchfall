"""Unit tests for the paper execution engine. No network.

Covers the verified taker-fee formula (fee = C x rate x p x (1-p)),
half-spread touch fills, book-walk fills, and partial fills.
"""

import pytest

from app.config import Settings
from app.execution.paper import (
    PaperEngine,
    Position,
    fee_rate_for,
    mark_to_market,
    taker_fee_usd,
)
from app.memory.audit import AuditLog
from app.policy.gate import Signal


def make_settings(**overrides):
    base = dict()
    base.update(overrides)
    return Settings(**base)


def make_signal(**overrides):
    base = dict(
        market_id="m1",
        token_id="t1",
        question="Will it rain tomorrow?",
        p_true=0.70,
        market_price=0.50,
        spread_cents=0.0,
        top_book_depth_usd=100_000.0,
        hours_to_resolution=48.0,
        news_ts="2026-09-30T00:00:00+00:00",
        price_ts="2026-09-30T00:10:00+00:00",
        negrisk_sum=1.0,
        uma_dispute=False,
        jev_confidence=0.8,
        jev_choice="YES",
        fee_category="Other",
        book_asks=[],
        book_bids=[],
    )
    base.update(overrides)
    return Signal(**base)


@pytest.fixture()
def engine(tmp_path):
    audit = AuditLog(str(tmp_path / "audit.db"))
    return PaperEngine(make_settings(), audit), audit


# --- fee math -------------------------------------------------------------- #
def test_fee_rate_table():
    assert fee_rate_for("Crypto") == pytest.approx(0.07)
    assert fee_rate_for("Politics") == pytest.approx(0.04)
    assert fee_rate_for("Sports") == pytest.approx(0.05)
    assert fee_rate_for("Geopolitics") == pytest.approx(0.0)
    assert fee_rate_for("SomethingUnknown") == pytest.approx(0.05)  # Other fallback
    assert fee_rate_for(None) == pytest.approx(0.05)


def test_taker_fee_formula_hand_computed():
    # 100 shares @ $0.50, Politics 0.04 -> 100*0.04*0.5*0.5 = $1.00
    assert taker_fee_usd(100.0, 0.50, 0.04) == pytest.approx(1.00)


def test_fee_formula_in_fill(engine):
    eng, audit = engine
    # $50 notional @ touch 0.50 -> 100 shares; Politics 0.04 -> $1.00 fee
    fill = eng.execute(make_signal(), 50.0, fee_category="Politics")
    assert fill.contracts == pytest.approx(100.0)
    assert fill.fee_usd == pytest.approx(1.00)
    assert fill.slippage_usd == pytest.approx(0.0)  # no spread, no walk
    assert fill.intended_price == pytest.approx(0.50)
    assert fill.price == pytest.approx(0.50)
    assert fill.filled_usd == pytest.approx(50.0)


def test_fee_rate_override_wins(engine):
    eng, _ = engine
    fill = eng.execute(
        make_signal(), 50.0, fee_category="Politics", fee_rate_override=0.02
    )
    # 100 shares @ 0.50 x 0.02 -> $0.50
    assert fill.fee_usd == pytest.approx(0.50)


# --- fill models ----------------------------------------------------------- #
def test_full_fill_at_touch(engine):
    eng, audit = engine
    fill = eng.execute(make_signal(spread_cents=2.0), 1000.0)
    # touch = 0.5 + 0.01 = 0.51 (taker crosses half-spread)
    assert fill.intended_price == pytest.approx(0.51)
    assert fill.price == pytest.approx(0.51)
    assert fill.side == "YES"
    assert fill.contracts == pytest.approx(1000.0 / 0.51)
    assert fill.filled_usd == pytest.approx(1000.0)
    # slippage = half-spread crossing: (0.51-0.50) x contracts
    assert fill.slippage_usd == pytest.approx(0.01 * (1000.0 / 0.51))
    assert "full fill at touch" in fill.note
    fills = audit.query("fill")
    assert len(fills) == 1
    assert fills[0]["payload"]["intended_price"] == pytest.approx(0.51)


def test_fill_no_side_uses_no_token_price(engine):
    eng, _ = engine
    fill = eng.execute(make_signal(p_true=0.40, market_price=0.60), 1000.0)
    assert fill.side == "NO"
    # NO token price 0.4; touch = 0.4 (no spread)
    assert fill.intended_price == pytest.approx(0.40)
    assert fill.price == pytest.approx(0.40)


def test_book_walk_fill(engine):
    eng, _ = engine
    # touch 0.52 (spread 4c on 0.50); depth $1070 -> 1000 > 1% -> walk
    sig = make_signal(
        spread_cents=4.0,
        book_asks=[[0.52, 1000.0], [0.55, 1000.0]],
    )
    fill = eng.execute(sig, 1000.0)
    shares_needed = 1000.0 / 0.52
    take1 = min(shares_needed, 1000.0)
    take2 = shares_needed - take1
    vwap = (take1 * 0.52 + take2 * 0.55) / shares_needed
    assert fill.price == pytest.approx(vwap)
    assert fill.price > fill.intended_price  # walk impact above touch
    assert fill.contracts == pytest.approx(shares_needed)
    # actual spend exceeds the touch-based estimate when walking the book
    assert fill.filled_usd == pytest.approx(fill.contracts * fill.price)
    assert fill.filled_usd > fill.size_usd
    assert "book-walk fill" in fill.note


def test_partial_fill_when_depth_short(engine):
    eng, _ = engine
    # only 200 shares rest on the take side -> partial
    sig = make_signal(
        spread_cents=4.0,
        book_asks=[[0.52, 100.0], [0.55, 100.0]],
    )
    fill = eng.execute(sig, 1000.0)
    assert fill.contracts == pytest.approx(200.0)
    assert fill.price == pytest.approx((0.52 * 100 + 0.55 * 100) / 200.0)
    assert fill.filled_usd == pytest.approx(200.0 * fill.price)
    assert fill.filled_usd < fill.size_usd
    assert "PARTIAL" in fill.note


def test_no_book_walk_below_threshold(engine):
    eng, _ = engine
    # deep book: 1000 < 1% of $104000 -> fill at touch, no walk
    sig = make_signal(
        spread_cents=4.0,
        book_asks=[[0.52, 200_000.0]],
    )
    fill = eng.execute(sig, 1000.0)
    assert fill.price == pytest.approx(0.52)
    assert "full fill at touch" in fill.note


def test_mark_to_market():
    positions = [
        Position(
            market_id="m1",
            token_id="t1",
            side="YES",
            contracts=100.0,
            avg_price=0.50,
            current_price=0.50,
            unrealized_pnl=0.0,
        )
    ]
    marked = mark_to_market(positions, lambda m, t, s: 0.60)
    assert marked[0].current_price == pytest.approx(0.60)
    assert marked[0].unrealized_pnl == pytest.approx(10.0)  # (0.6-0.5)*100
    # original untouched
    assert positions[0].unrealized_pnl == 0.0
