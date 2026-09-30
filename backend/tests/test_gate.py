"""Unit tests for the deterministic risk gate. No network.

Covers the verified rule set: quarter-Kelly sizing (hand-computed),
fee-adjusted edge, and every new veto threshold (3c spread, $50 depth,
2h to resolution, 15-min news/price skew, negrisk band, 0.6 confidence,
SKIP, UMA dispute, 5 concurrent positions).
"""

import pytest

from app.config import Settings
from app.policy.gate import GateResult, RiskGate, Signal, buy_direction, kelly_fraction


def make_settings(**overrides):
    base = dict(
        EDGE_THRESHOLD=0.10,
        KELLY_FRACTION=0.25,
        MAX_PER_MARKET_FRAC=0.10,
        MAX_TOTAL_EXPOSURE_FRAC=0.40,
        MAX_CONCURRENT_POSITIONS=5,
        SPREAD_VETO_CENTS=0.03,
        MIN_TOP_DEPTH_USD=50.0,
        MIN_HOURS_TO_RESOLUTION=2.0,
        MAX_NEWS_PRICE_SKEW_SEC=900,
        NEGRISK_SUM_MIN=0.98,
        NEGRISK_SUM_MAX=1.02,
        JEV_CONFIDENCE_MIN=0.6,
    )
    base.update(overrides)
    return Settings(**base)


def make_signal(**overrides):
    base = dict(
        market_id="m1",
        token_id="t1",
        question="Will it rain tomorrow?",
        p_true=0.70,
        market_price=0.50,
        spread_cents=2.0,
        top_book_depth_usd=5000.0,
        hours_to_resolution=48.0,
        news_ts="2026-09-30T00:00:00+00:00",
        price_ts="2026-09-30T00:10:00+00:00",  # skew 600s < 900
        negrisk_sum=1.00,
        uma_dispute=False,
        jev_confidence=0.80,
        jev_choice="YES",
        fee_category="Other",
    )
    base.update(overrides)
    return Signal(**base)


def reasons(result: GateResult) -> list[str]:
    return [v.reason for v in result.vetoes]


def evaluate_ok(gate, signal, **kw):
    kw.setdefault("current_exposure_usd", 0.0)
    kw.setdefault("bankroll_usd", 10000.0)
    kw.setdefault("open_positions_count", 0)
    return gate.evaluate(signal, **kw)


# --- Kelly math ------------------------------------------------------------ #
def test_kelly_yes_hand_computed():
    # price=0.5, p=0.7, frac=0.25: b=1, f*=(0.7*1-0.3)/1=0.4 -> f=0.1
    assert kelly_fraction(0.7, 0.5, 0.25) == pytest.approx(0.10)


def test_kelly_no_side_hand_computed():
    # buy NO at YES-price 0.6, p_true 0.4 -> p_no=0.6, price_no=0.4, b=1.5
    # f* = (0.6*1.5 - 0.4)/1.5 = 0.5/1.5 = 1/3; quarter-Kelly = 1/12
    assert kelly_fraction(0.6, 0.4, 0.25) == pytest.approx(1 / 12)


def test_kelly_zero_or_negative_edge():
    assert kelly_fraction(0.5, 0.5, 0.25) == 0.0  # no edge
    assert kelly_fraction(0.3, 0.5, 0.25) == 0.0  # negative edge floored
    assert kelly_fraction(0.7, 0.0, 0.25) == 0.0  # degenerate price
    assert kelly_fraction(0.7, 1.0, 0.25) == 0.0


def test_quarter_kelly_hand_computed_gate():
    # P=0.80, price=0.62 -> f* = (0.80-0.62)/(1-0.62) = 0.18/0.38 = 0.4737
    # quarter-Kelly bet = 0.4737/4 x bankroll = 0.1184 x bankroll.
    # (cap lifted to 0.20 so the raw Kelly value is observable)
    gate = RiskGate(make_settings(MAX_PER_MARKET_FRAC=0.20))
    result = evaluate_ok(
        gate, make_signal(p_true=0.80, market_price=0.62, jev_choice="YES")
    )
    assert result.approved
    # fee check: Other 0.05 x (1-0.62) = 0.019 drag; 0.18-0.019=0.161 >= 0.10
    assert result.fee_adjusted_edge == pytest.approx(0.161)
    assert result.size_usd == pytest.approx(0.118421 * 10000.0, rel=1e-4)


def test_kelly_clamp_to_max_per_market():
    gate = RiskGate(make_settings())
    # p=0.99 @ 0.5 -> f* = 0.98, quarter = 0.245, clamped to 0.10
    result = evaluate_ok(gate, make_signal(p_true=0.99))
    assert result.approved
    assert result.size_usd == pytest.approx(1000.0)


def test_buy_direction():
    assert buy_direction(make_signal(p_true=0.7, market_price=0.5)) == "YES"
    assert buy_direction(make_signal(p_true=0.4, market_price=0.5)) == "NO"


# --- fee-adjusted edge ----------------------------------------------------- #
def test_fee_adjusted_edge_veto():
    # raw edge 0.10, but Other 0.05 x (1-0.50) = 0.025 fee drag
    # -> 0.075 < 0.10 => veto
    gate = RiskGate(make_settings())
    result = evaluate_ok(gate, make_signal(p_true=0.60, market_price=0.50))
    assert not result.approved
    assert "edge_too_small" in reasons(result)
    assert result.fee_adjusted_edge == pytest.approx(0.075)


def test_geopolitics_zero_fee_passes_raw_edge():
    # Geopolitics fee = 0 -> adjusted edge == raw edge 0.15 -> passes
    gate = RiskGate(make_settings())
    result = evaluate_ok(
        gate,
        make_signal(p_true=0.65, market_price=0.50, fee_category="Geopolitics"),
    )
    assert result.approved
    assert result.fee_adjusted_edge == pytest.approx(0.15)


# --- each veto condition --------------------------------------------------- #
def test_wide_spread_veto_cents():
    gate = RiskGate(make_settings())
    # 3.0c is the boundary -> passes; 3.1c -> veto
    assert evaluate_ok(gate, make_signal(spread_cents=3.0)).approved
    result = evaluate_ok(gate, make_signal(spread_cents=3.1))
    assert not result.approved
    assert "wide_spread" in reasons(result)


def test_thin_book_veto():
    gate = RiskGate(make_settings())
    assert evaluate_ok(gate, make_signal(top_book_depth_usd=50.0)).approved
    result = evaluate_ok(gate, make_signal(top_book_depth_usd=49.99))
    assert not result.approved
    assert "thin_book" in reasons(result)


def test_near_resolution_veto():
    gate = RiskGate(make_settings())
    assert evaluate_ok(gate, make_signal(hours_to_resolution=2.0)).approved
    result = evaluate_ok(gate, make_signal(hours_to_resolution=1.9))
    assert not result.approved
    assert "near_resolution" in reasons(result)


def test_news_price_skew_veto():
    gate = RiskGate(make_settings())
    # 900s skew is the boundary -> passes; 901s -> veto
    ok = make_signal(
        news_ts="2026-09-30T00:00:00+00:00",
        price_ts="2026-09-30T00:15:00+00:00",
    )
    assert evaluate_ok(gate, ok).approved
    bad = make_signal(
        news_ts="2026-09-30T00:00:00+00:00",
        price_ts="2026-09-30T00:15:01+00:00",
    )
    result = evaluate_ok(gate, bad)
    assert not result.approved
    assert "news_price_skew" in reasons(result)


def test_news_price_skew_unknown_veto():
    gate = RiskGate(make_settings())
    result = evaluate_ok(gate, make_signal(news_ts="", price_ts=""))
    assert not result.approved
    assert "news_price_skew_unknown" in reasons(result)


def test_negrisk_sum_veto():
    gate = RiskGate(make_settings())
    assert evaluate_ok(gate, make_signal(negrisk_sum=0.98)).approved
    assert evaluate_ok(gate, make_signal(negrisk_sum=1.02)).approved
    for bad_sum in (0.97, 1.03, 0.0, None):
        result = evaluate_ok(gate, make_signal(negrisk_sum=bad_sum))
        assert not result.approved, f"negrisk_sum={bad_sum} should veto"
        assert "negrisk_sum_invalid" in reasons(result)


def test_uma_dispute_veto():
    gate = RiskGate(make_settings())
    result = evaluate_ok(gate, make_signal(uma_dispute=True))
    assert not result.approved
    assert "uma_dispute" in reasons(result)


def test_low_jev_confidence_veto():
    gate = RiskGate(make_settings())
    assert evaluate_ok(gate, make_signal(jev_confidence=0.6)).approved
    result = evaluate_ok(gate, make_signal(jev_confidence=0.59))
    assert not result.approved
    assert "low_jev_confidence" in reasons(result)


def test_model_skip_veto():
    gate = RiskGate(make_settings())
    result = evaluate_ok(gate, make_signal(jev_choice="SKIP"))
    assert not result.approved
    assert "model_skip" in reasons(result)


def test_max_positions_veto():
    gate = RiskGate(make_settings())
    assert evaluate_ok(gate, make_signal(), open_positions_count=4).approved
    result = evaluate_ok(gate, make_signal(), open_positions_count=5)
    assert not result.approved
    assert "max_positions" in reasons(result)


def test_exposure_cap_veto():
    gate = RiskGate(make_settings())
    # default approved size is $1000 (Kelly 0.10 clamped); 3200+1000 > 4000
    result = evaluate_ok(gate, make_signal(), current_exposure_usd=3200.0)
    assert not result.approved
    assert "exposure_cap" in reasons(result)


def test_bad_inputs_veto():
    gate = RiskGate(make_settings())
    result = evaluate_ok(gate, make_signal(market_price=1.5))
    assert not result.approved
    assert "bad_inputs" in reasons(result)


def test_all_vetoes_collected_no_short_circuit():
    gate = RiskGate(make_settings())
    result = gate.evaluate(
        make_signal(
            p_true=0.52,  # raw edge 0.02 -> edge_too_small after fee
            spread_cents=5.0,  # wide_spread
            top_book_depth_usd=10.0,  # thin_book
            hours_to_resolution=1.0,  # near_resolution
            news_ts="2026-09-30T00:00:00+00:00",
            price_ts="2026-09-30T01:00:00+00:00",  # news_price_skew
            negrisk_sum=0.5,  # negrisk_sum_invalid
            uma_dispute=True,  # uma_dispute
            jev_confidence=0.1,  # low_jev_confidence
        ),
        current_exposure_usd=0.0,
        bankroll_usd=10000.0,
        open_positions_count=9,  # max_positions
    )
    assert not result.approved
    for code in (
        "edge_too_small",
        "wide_spread",
        "thin_book",
        "near_resolution",
        "news_price_skew",
        "negrisk_sum_invalid",
        "uma_dispute",
        "low_jev_confidence",
        "max_positions",
    ):
        assert code in reasons(result)
