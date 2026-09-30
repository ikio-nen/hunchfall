"""Write playtest reports (JSON + CSV) to playtest_reports/."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from app.playtest.runner import RoundReport

CSV_FIELDS = [
    "question",
    "outcome",
    "label",
    "market_price",
    "p_true",
    "edge",
    "action",
    "correct",
    "brier",
    "baseline_side",
    "baseline_correct",
    "error",
]


def write_reports(
    report: RoundReport,
    out_dir: str | Path | None = None,
    prefix: str = "round",
) -> tuple[str, str]:
    """Write the round report as JSON and per-example CSV.

    Args:
        report: RoundReport from :func:`run_round`.
        out_dir: Destination dir (default: backend/playtest_reports/).
        prefix: Filename prefix; a UTC timestamp is appended.

    Returns:
        ``(json_path, csv_path)`` as strings.
    """
    root = (
        Path(out_dir)
        if out_dir
        else Path(__file__).resolve().parents[2] / "playtest_reports"
    )
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    json_path = root / f"{prefix}-{stamp}.json"
    csv_path = root / f"{prefix}-{stamp}.csv"

    json_path.write_text(json.dumps(report.to_dict(), indent=2, default=str))

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in report.details:
            writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})

    return str(json_path), str(csv_path)
