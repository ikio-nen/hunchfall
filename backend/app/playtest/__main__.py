"""Offline playtest harness entrypoint.

``python -m app.playtest --samples`` runs the harness on the bundled
synthetic samples with a deterministic MOCK decide_fn (clearly labeled as
a mock — never a real model judgment) and writes JSON + CSV reports to
playtest_reports/.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from app.playtest.report import write_reports
from app.playtest.runner import compare_to_baseline, run_round
from app.playtest.schema import LabeledExample, load_jsonl

SAMPLES_PATH = Path(__file__).resolve().parent / "samples" / "samples.jsonl"


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
    """CLI: ``--samples`` or ``--input <jsonl>``."""
    parser = argparse.ArgumentParser(
        description="hunchfall offline playtest harness (mock decisions)"
    )
    parser.add_argument(
        "--samples", action="store_true", help="run on bundled synthetic samples"
    )
    parser.add_argument("--input", default=None, help="JSONL file of labeled examples")
    args = parser.parse_args(argv)

    path = args.input or (str(SAMPLES_PATH) if args.samples else None)
    if not path:
        parser.error("pass --samples or --input <jsonl>")
    examples = load_jsonl(path)
    if not examples:
        print(json.dumps({"error": f"no examples loaded from {path}"}))
        return 1

    report = run_round(examples, mock_decide, lambda ex: ex.odds_snapshot_yes)
    json_path, csv_path = write_reports(report)
    print(
        json.dumps(
            {
                "mode": "PLAYTEST",
                "decide_fn": "mock_decide (MOCK — not a real model)",
                "n_examples": report.n_examples,
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
