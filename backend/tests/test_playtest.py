"""Unit tests for the playtest harness. No network."""

from pathlib import Path

import pytest

from app.playtest.runner import compare_to_baseline, run_round
from app.playtest.schema import LabeledExample, load_jsonl, save_jsonl
from app.playtest.split import train_test_split

SAMPLES = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "playtest"
    / "samples"
    / "samples.jsonl"
)


def make_example(question, outcome, price):
    return LabeledExample(
        news_snapshot="snapshot",
        market_question=question,
        odds_snapshot_yes=price,
        outcome=outcome,
        source="test",
        label="SAMPLE",
    )


def test_split_sizes_and_stratification():
    examples = [make_example(f"q{i}", "YES", 0.6) for i in range(6)] + [
        make_example(f"n{i}", "NO", 0.4) for i in range(4)
    ]
    tuning, test = train_test_split(examples, test_frac=0.3, seed=42)
    # round(6*0.3)=2 YES + round(4*0.3)=1 NO in test
    assert len(test) == 3
    assert len(tuning) == 7
    assert sum(1 for e in test if e.outcome == "YES") == 2
    assert sum(1 for e in test if e.outcome == "NO") == 1
    # deterministic
    tuning2, test2 = train_test_split(examples, test_frac=0.3, seed=42)
    assert [e.market_question for e in test] == [e.market_question for e in test2]
    assert [e.market_question for e in tuning] == [
        e.market_question for e in tuning2
    ]


def test_metrics_hand_computed():
    # ex1: YES @0.6, p=0.8 -> bet YES, correct
    # ex2: NO  @0.4, p=0.2 -> bet NO,  correct
    # ex3: YES @0.55, p=0.58 -> edge 0.03 < 0.1 -> abstain
    # ex4: NO  @0.7, p=0.4 -> bet NO,  correct
    examples = [
        make_example("e1", "YES", 0.60),
        make_example("e2", "NO", 0.40),
        make_example("e3", "YES", 0.55),
        make_example("e4", "NO", 0.70),
    ]
    p_by_q = {"e1": 0.8, "e2": 0.2, "e3": 0.58, "e4": 0.4}
    report = run_round(
        examples,
        decide_fn=lambda ex: p_by_q[ex.market_question],
        price_fn=lambda ex: ex.odds_snapshot_yes,
        edge_threshold=0.10,
    )
    assert report.n_examples == 4
    assert report.n_bets == 3
    assert report.n_abstentions == 1
    assert report.abstention_rate == pytest.approx(0.25)
    assert report.accuracy == pytest.approx(1.0)
    # brier = (0.04 + 0.04 + 0.16) / 3 = 0.08
    assert report.brier_score == pytest.approx(0.08)
    # baseline bets favorite: YES, NO, YES, YES -> 3/4 correct
    assert report.baseline_accuracy == pytest.approx(0.75)
    assert len(report.calibration_bins) == 10
    # calibration covers every produced P(true), including the abstention
    assert sum(b["n"] for b in report.calibration_bins) == 4

    comp = compare_to_baseline(report)
    assert comp["accuracy_delta"] == pytest.approx(0.25)
    assert "beats baseline" in comp["verdict"]


def test_compare_no_bets():
    examples = [make_example("e1", "YES", 0.55)]
    report = run_round(
        examples,
        decide_fn=lambda ex: 0.58,  # edge 0.03 -> abstain
        price_fn=lambda ex: ex.odds_snapshot_yes,
    )
    assert report.accuracy is None
    comp = compare_to_baseline(report)
    assert "no bets placed" in comp["verdict"]


def test_samples_load_and_roundtrip(tmp_path):
    examples = load_jsonl(SAMPLES)
    samples = [e for e in examples if e.label == "SAMPLE"]
    real = [e for e in examples if e.label == "REAL"]

    # the hand-written synthetic rows are pinned
    assert len(samples) == 5
    assert all(e.source == "synthetic-hand-written" for e in samples)
    assert {e.outcome for e in samples} == {"YES", "NO"}
    # every synthetic example is clearly labelled as such
    assert all("SYNTHETIC" in e.news_snapshot for e in samples)

    # the REAL set is genuinely labelled data (docs/PLAYTESTING.md round 1)
    assert len(real) >= 20
    assert {e.outcome for e in real} <= {"YES", "NO"}
    assert len({e.market_question for e in real}) == len(real)  # no duplicates
    for e in real:
        assert e.source.startswith("REAL")
        # decision-time price is a real, non-degenerate quote
        assert 0.02 <= e.odds_snapshot_yes <= 0.98
        # the snapshot is market metadata, and says so — it is not news
        assert "not news" in e.news_snapshot
        assert e.market_question

    out = tmp_path / "rt.jsonl"
    save_jsonl(examples, out)
    reloaded = load_jsonl(out)
    assert [e.to_dict() for e in reloaded] == [e.to_dict() for e in examples]
