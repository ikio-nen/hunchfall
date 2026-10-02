"""Offline playtest harness entrypoint.

``python -m app.playtest --samples`` runs the harness on the bundled samples
with a deterministic MOCK decide_fn and writes JSON + CSV reports to
playtest_reports/.

The ``--split`` flag implements the protocol in docs/PLAYTESTING.md: the
labeled set is divided by ``split.train_test_split`` (seeded, stratified by
outcome) and only the requested side is scored, so the held-out test split is
never touched while tuning.

IMPORTANT: the bundled ``mock_decide`` is **not a model**. It derives
P(true) from ``sha256(market_question)``, so its accuracy and Brier score
measure a hash function and carry no model evidence. The harness labels this
in its output; report those numbers as MOCK, never as validation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from app.playtest.report import write_reports
from app.playtest.runner import compare_to_baseline, run_round
from app.playtest.schema import LabeledExample, load_jsonl
from app.playtest.split import train_test_split

SAMPLES_PATH = Path(__file__).resolve().parent / "samples" / "samples.jsonl"

MOCK_DECIDE_NOTE = (
    "derived from sha256(market_question) - these numbers measure a hash, "
    "not a model, and are NOT validation evidence"
)


def mock_decide(example: LabeledExample) -> float | None:
    """Deterministic MOCK P(true). MOCK — not a real model.

    Abstains (returns None) when the snapshot is contradictory/ambiguous/
    unclear — mirroring the SKIP path. Otherwise derives a fixed
    pseudo-probability from the question hash so runs are reproducible.
    """
    text = f"{example.news_snapshot} {example.market_question}".lower()
    if any(w in text for w in ("contradictory", "ambiguous", "unclear")):
        return None
    h = int(hashlib.sha256(example.market_question.encode()).hexdigest(), 16)
    return 0.15 + 0.70 * ((h % 1000) / 1000.0)


def main(argv: list[str] | None = None) -> int:
    """CLI: ``--samples`` or ``--input/--file <jsonl>``, optionally ``--split``."""
    parser = argparse.ArgumentParser(
        description="hunchfall offline playtest harness (mock decisions)"
    )
    parser.add_argument(
        "--samples", action="store_true", help="run on bundled synthetic samples"
    )
    parser.add_argument(
        "--input",
        "--file",
        dest="input",
        default=None,
        help="JSONL file of labeled examples",
    )
    parser.add_argument(
        "--split",
        choices=("all", "tuning", "test"),
        default="all",
        help="score every row, or only the tuning / held-out test side (split.py)",
    )
    parser.add_argument(
        "--test-frac", type=float, default=0.3, help="held-out fraction for --split"
    )
    parser.add_argument(
        "--label", default=None, help="only rows with this label (e.g. REAL, SAMPLE)"
    )
    parser.add_argument("--prefix", default="round", help="report filename prefix")
    args = parser.parse_args(argv)

    path = args.input or (str(SAMPLES_PATH) if args.samples else None)
    if not path:
        parser.error("pass --samples or --input <jsonl>")
    examples = load_jsonl(path)
    if args.label:
        examples = [ex for ex in examples if ex.label == args.label]
    if not examples:
        print(json.dumps({"error": f"no examples loaded from {path}"}))
        return 1

    tuning, test = train_test_split(examples, test_frac=args.test_frac)
    selected = {"all": examples, "tuning": tuning, "test": test}[args.split]

    report = run_round(selected, mock_decide, lambda ex: ex.odds_snapshot_yes)
    json_path, csv_path = write_reports(report, prefix=f"{args.prefix}-{args.split}")
    print(
        json.dumps(
            {
                "mode": "PLAYTEST",
                "decide_fn": "mock_decide (MOCK — not a real model)",
                "decide_fn_note": MOCK_DECIDE_NOTE,
                "input": path,
                "label_filter": args.label,
                "split": args.split,
                "test_frac": args.test_frac,
                "n_loaded": len(examples),
                "n_tuning": len(tuning),
                "n_test": len(test),
                "n_evaluated": report.n_examples,
                "accuracy": report.accuracy,
                "brier_score": report.brier_score,
                "abstention_rate": report.abstention_rate,
                "baseline_accuracy": report.baseline_accuracy,
                "calibration_bins": report.calibration_bins,
                "report_json": json_path,
                "report_csv": csv_path,
                "comparison": compare_to_baseline(report),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
