"""Playtest round runner: scores a decision function on labeled examples.

For each example the harness:
  * asks ``decide_fn`` for P(true) (``None`` = abstain / SKIP),
  * reads the market YES price from ``price_fn``,
  * applies the edge rule (bet only if ``|p - price| >= edge_threshold``),
  * scores a naive baseline alongside (bet the market favorite).

Metrics: accuracy (on bets placed), abstention rate, Brier score, baseline
accuracy, and 10-bin calibration (mean predicted vs empirical frequency).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable

from app.playtest.schema import LabeledExample

DecideFn = Callable[[LabeledExample], float | None]
PriceFn = Callable[[LabeledExample], float]


@dataclass
class RoundReport:
    """Metrics for one playtest round."""

    n_examples: int
    n_bets: int
    n_abstentions: int
    accuracy: float | None  # correct / bets placed; None when no bets
    abstention_rate: float
    brier_score: float | None  # mean (p - outcome)^2 over bets; None if none
    baseline_accuracy: float | None  # naive favorite-betting accuracy
    calibration_bins: list[dict]  # 10 bins: n, mean_predicted, empirical_freq
    details: list[dict]  # per-example rows

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict."""
        return asdict(self)


def _calibration_bins(details: list[dict], n_bins: int = 10) -> list[dict]:
    """Bin predicted P(true) values; compare mean predicted vs empirical YES freq.

    Covers every produced prediction, including abstentions (abstention is an
    edge-rule outcome, not a model failure, so the model stays calibrated on
    the full set it scored).
    """
    bins: list[dict] = []
    for i in range(n_bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        in_bin = [
            r
            for r in details
            if r.get("p_true") is not None and lo <= r["p_true"] < hi + 1e-12
        ]
        # fix the top edge: p == 1.0 belongs in the last bin only
        if i < n_bins - 1:
            in_bin = [r for r in in_bin if r["p_true"] < hi]
        n = len(in_bin)
        bins.append(
            {
                "bin": f"[{lo:.1f}, {hi:.1f}]",
                "n": n,
                "mean_predicted": (
                    sum(r["p_true"] for r in in_bin) / n if n else None
                ),
                "empirical_freq": (
                    sum(1.0 for r in in_bin if r["outcome"] == "YES") / n
                    if n
                    else None
                ),
            }
        )
    return bins


def run_round(
    examples: list[LabeledExample],
    decide_fn: DecideFn,
    price_fn: PriceFn,
    edge_threshold: float = 0.10,
) -> RoundReport:
    """Score a decision function on labeled examples.

    Args:
        examples: Labeled examples to play.
        decide_fn: ``example -> P(true)`` in [0, 1], or None to abstain.
        price_fn: ``example -> market YES price`` in [0, 1].
        edge_threshold: Minimum |p - price| to place a bet.

    Returns:
        RoundReport with metrics and per-example details. A decide_fn
        exception on one example is recorded and counts as an abstention —
        the round never dies on a single bad example.
    """
    details: list[dict] = []
    for ex in examples:
        row: dict = {
            "question": ex.market_question,
            "outcome": ex.outcome,
            "label": ex.label,
        }
        try:
            price = float(price_fn(ex))
            p = decide_fn(ex)
            p = None if p is None else float(p)
        except Exception as exc:  # noqa: BLE001 - recorded, counts as abstain
            row.update({"action": "abstain", "error": str(exc)})
            details.append(row)
            continue
        row["market_price"] = price
        row["p_true"] = p
        outcome_bin = 1.0 if ex.outcome == "YES" else 0.0
        if p is None or abs(p - price) < edge_threshold:
            row["action"] = "abstain"
        else:
            row["edge"] = abs(p - price)
            side = "YES" if p > price else "NO"
            row["action"] = f"bet_{side}"
            row["correct"] = side == ex.outcome
            row["brier"] = (p - outcome_bin) ** 2
        base_side = "YES" if price >= 0.5 else "NO"
        row["baseline_side"] = base_side
        row["baseline_correct"] = base_side == ex.outcome
        details.append(row)

    bets = [r for r in details if str(r.get("action", "")).startswith("bet")]
    n = len(details)
    n_bets = len(bets)
    accuracy = (
        sum(1.0 for r in bets if r.get("correct")) / n_bets if n_bets else None
    )
    brier = sum(r["brier"] for r in bets) / n_bets if n_bets else None
    baseline_accuracy = (
        sum(1.0 for r in details if r.get("baseline_correct")) / n if n else None
    )
    return RoundReport(
        n_examples=n,
        n_bets=n_bets,
        n_abstentions=n - n_bets,
        accuracy=accuracy,
        abstention_rate=(n - n_bets) / n if n else 0.0,
        brier_score=brier,
        baseline_accuracy=baseline_accuracy,
        calibration_bins=_calibration_bins(details),
        details=details,
    )


def compare_to_baseline(report: RoundReport) -> dict:
    """Compare the model's round against the naive market-favorite baseline.

    Args:
        report: RoundReport from :func:`run_round`.

    Returns:
        Dict with accuracies, delta, and a plain-language verdict. Never
        claims a win when no bets were placed.
    """
    model = report.accuracy
    base = report.baseline_accuracy
    delta = None
    if model is not None and base is not None:
        delta = model - base
    if report.n_bets == 0:
        verdict = "no bets placed — cannot compare to baseline"
    elif delta is None:
        verdict = "comparison unavailable"
    elif delta > 0:
        verdict = f"model beats baseline by {delta:+.3f} accuracy"
    elif delta < 0:
        verdict = f"model trails baseline by {delta:+.3f} accuracy"
    else:
        verdict = "model ties baseline accuracy"
    return {
        "model_accuracy": model,
        "baseline_accuracy": base,
        "accuracy_delta": delta,
        "n_bets": report.n_bets,
        "abstention_rate": report.abstention_rate,
        "brier_score": report.brier_score,
        "verdict": verdict,
    }
