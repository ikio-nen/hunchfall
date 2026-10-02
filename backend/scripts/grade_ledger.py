"""Automatic resolution grading for the prediction ledger (RFC-003).

Walks every logged ``predict`` event that has no ``predict_resolved`` event
yet, resolves its market through Gamma, and — only when a winner is
**confirmed** — appends a ``predict_resolved`` event (``source: "gamma"``) so
``/predict/accuracy`` reflects the verdict with no manual step.

Honesty rules (never guessed):

* an open market (``closed`` false) is left ungraded and retried next run;
* the documented ``["0","0"]`` resolution shape, any zero-winner shape, and
  any outcome name other than Yes/No are **ambiguous** — skipped, never
  graded;
* a Gamma failure is recorded as a ``stage_error`` and does not abort the run.

Idempotent: a prediction that already has a ``predict_resolved`` event (a
manual settlement, or an earlier grading pass) is never graded twice, so the
ledger never double-counts.

The run writes ``grade-<UTC stamp>.json`` + ``.md`` into the existing
``backend/playtest_reports/`` directory and prints a compact summary.

Usage (from ``backend/``)::

    python scripts/grade_ledger.py                 # grade + write reports
    python scripts/grade_ledger.py --no-write      # dry metrics only
    python scripts/grade_ledger.py --include-mock  # include mock predictions
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.memory.audit import AuditLog  # noqa: E402
from app.paths import resolve_db_path, utcnow_iso  # noqa: E402
from app.polymarket.gamma import (  # noqa: E402
    GammaClient,
    parse_outcome_prices,
)
from app.predict import PAPER_LABEL, accuracy_report  # noqa: E402

#: A confirmed winner's outcome price. A closed market with any other price
#: shape (including the documented ``["0","0"]``) has no confirmed winner.
WINNER_PRICE = 0.999

SKIP_KEYS = (
    "already_graded",
    "no_condition_id",
    "unresolved",
    "not_found",
    "ambiguous",
    "errors",
)


def parse_outcomes(market: dict) -> list[str]:
    """Parse Gamma ``outcomes`` into a list of names (JSON-string aware).

    Like ``outcomePrices``, ``outcomes`` usually arrives as a JSON *string*
    (``'["Yes", "No"]'``); a real list is accepted as-is. Unparseable input
    yields [] and the caller treats the market as ambiguous.

    Args:
        market: Gamma market dict.

    Returns:
        Outcome names, position-aligned with ``outcomePrices``.
    """
    raw = market.get("outcomes")
    if isinstance(raw, (list, tuple)):
        return [str(outcome) for outcome in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [str(outcome) for outcome in parsed]
    return []


def tag_labels(market: dict) -> list[str]:
    """Tag labels from a Gamma market payload (deduped, order kept).

    Args:
        market: Gamma market dict (``include_tag=true`` shape: dicts with
            ``label``/``slug``; bare strings are accepted too).

    Returns:
        Tag labels; [] when the payload carries none.
    """
    labels: list[str] = []
    for tag in market.get("tags") or []:
        label = str(tag.get("label") if isinstance(tag, dict) else tag).strip()
        if label and label not in labels:
            labels.append(label)
    return labels


def resolve_market_outcome(market: dict) -> tuple[str | None, str]:
    """Map a Gamma market payload to a confirmed outcome, or an explicit skip.

    Args:
        market: Gamma market dict ({} when the condition id matched nothing).

    Returns:
        ``(outcome, reason)``. ``outcome`` is ``"YES"``/``"NO"`` exactly when
        ``reason == "resolved"``; otherwise outcome is None and reason is one
        of ``not_found`` | ``unresolved`` | ``ambiguous`` — a skip, never a
        guess.
    """
    if not market:
        return None, "not_found"
    if not market.get("closed"):
        return None, "unresolved"
    outcomes = parse_outcomes(market)
    prices = parse_outcome_prices(market)
    if len(outcomes) != len(prices) or len(prices) < 2:
        return None, "ambiguous"
    winners = [index for index, price in enumerate(prices) if price >= WINNER_PRICE]
    if len(winners) != 1:
        # ["0","0"] is the documented gotcha: no winner, and mid-resolution
        # prices are not a verdict either.
        return None, "ambiguous"
    name = outcomes[winners[0]].strip().lower()
    if name == "yes":
        return "YES", "resolved"
    if name == "no":
        return "NO", "resolved"
    return None, "ambiguous"


def _latest_resolutions(resolutions: list[dict]) -> dict[str, dict]:
    """Newest ``predict_resolved`` event per prediction id (mirrors accuracy_report)."""
    latest: dict[str, dict] = {}
    for event in resolutions:
        payload = event.get("payload") or {}
        pid = str(payload.get("prediction_id") or "")
        if not pid:
            continue
        previous = latest.get(pid)
        if previous is None or (event.get("id") or 0) > (previous.get("id") or 0):
            latest[pid] = event
    return latest


def category_breakdown(
    predictions: list[dict],
    resolutions: list[dict],
    *,
    include_mock: bool = False,
    limit: int = 5000,
) -> list[dict]:
    """Per-tag metrics over graded predictions whose market carried tags.

    A graded prediction counts in **every** tag its market carried (a market
    can genuinely be both ``Crypto`` and ``Bitcoin``); the tag filter is the
    only difference from ``accuracy_report``, whose math is reused per group
    so the numbers are identical to the headline ones.

    Args:
        predictions: ``predict`` event dicts.
        resolutions: ``predict_resolved`` event dicts (gamma-sourced carry
            ``tags``; manual ones do not and are therefore ungrouped).
        include_mock: Count mock predictions (mirrors the headline report).
        limit: Max predictions scanned per group.

    Returns:
        One dict per tag, sorted by tag name: ``n_resolved``,
        ``n_resolved_guessed``, ``direction_accuracy``, ``brier``,
        ``abstention_rate``.
    """
    latest = _latest_resolutions(resolutions)
    groups: dict[str, dict[str, list]] = {}
    for event in predictions:
        payload = event.get("payload") or {}
        pid = str(payload.get("prediction_id") or "")
        resolution = latest.get(pid)
        if resolution is None:
            continue
        tags = [
            str(tag)
            for tag in (resolution.get("payload") or {}).get("tags") or []
            if str(tag)
        ]
        for tag in tags:
            group = groups.setdefault(tag, {"predictions": [], "resolutions": []})
            group["predictions"].append(event)
            group["resolutions"].append(resolution)
    out: list[dict] = []
    for tag in sorted(groups):
        group = groups[tag]
        report = accuracy_report(
            group["predictions"],
            group["resolutions"],
            include_mock=include_mock,
            limit=limit,
        )
        out.append(
            {
                "tag": tag,
                "n_resolved": report["n_resolved"],
                "n_resolved_guessed": report["n_resolved_guessed"],
                "direction_accuracy": report["direction_accuracy"],
                "brier": report["brier"],
                "abstention_rate": report["abstention_rate"],
            }
        )
    return out


def grade_ledger(
    db_path: str | Path,
    *,
    gamma: GammaClient | None = None,
    limit: int = 5000,
    include_mock: bool = False,
    write: bool = True,
    out_dir: str | Path | None = None,
) -> dict:
    """Grade every ungraded prediction in the ledger, then report.

    Args:
        db_path: Audit SQLite path.
        gamma: Gamma client (tests inject a fake; None builds the real one).
        limit: Max predictions/resolutions scanned.
        include_mock: Include mock predictions in the metrics.
        write: Write the JSON + markdown report (default True).
        out_dir: Report directory (default ``backend/playtest_reports/``).

    Returns:
        The full report dict; ``report_paths`` names the written files (both
        None with ``write=False``).
    """
    audit = AuditLog(Path(db_path))
    owns_gamma = gamma is None
    if gamma is None:
        gamma = GammaClient(Settings())
    try:
        predictions = audit.predictions(limit=limit)
        graded_ids = {
            str((event.get("payload") or {}).get("prediction_id") or "")
            for event in audit.prediction_resolutions(limit=limit)
        }

        skipped = {key: 0 for key in SKIP_KEYS}
        graded_now: list[dict] = []
        for event in reversed(predictions):  # oldest first: stable audit trail
            payload = event.get("payload") or {}
            pid = str(payload.get("prediction_id") or "")
            if pid and pid in graded_ids:
                skipped["already_graded"] += 1
                continue
            condition_id = str(payload.get("condition_id") or "").strip()
            if not pid or not condition_id:
                skipped["no_condition_id"] += 1
                continue
            try:
                market = gamma.get_market_by_condition_id(condition_id)
            except Exception as exc:  # noqa: BLE001 - one market, never fatal
                skipped["errors"] += 1
                audit.record(
                    "stage_error",
                    {
                        "stage": "grade_ledger.gamma",
                        "condition_id": condition_id,
                        "error": str(exc),
                    },
                )
                continue
            outcome, reason = resolve_market_outcome(market)
            if outcome is None:
                skipped[reason] += 1
                continue
            tags = tag_labels(market)
            audit.record(
                "predict_resolved",
                {
                    "prediction_id": pid,
                    "outcome": outcome,
                    "resolved_at": utcnow_iso(),
                    "source": "gamma",
                    "condition_id": condition_id,
                    "tags": tags,
                },
            )
            graded_ids.add(pid)
            graded_now.append(
                {
                    "prediction_id": pid,
                    "condition_id": condition_id,
                    "outcome": outcome,
                    "tags": tags,
                }
            )

        # Re-read so the headline metrics reflect the events just written.
        fresh_predictions = audit.predictions(limit=limit)
        fresh_resolutions = audit.prediction_resolutions(limit=limit)
        report = {
            "mode": "PAPER",
            "as_of": utcnow_iso(),
            "db": str(db_path),
            "parameters": {"limit": limit, "include_mock": include_mock},
            "graded_now": len(graded_now),
            "graded": graded_now,
            "skipped": skipped,
            "ledger": accuracy_report(
                fresh_predictions,
                fresh_resolutions,
                include_mock=include_mock,
                limit=limit,
            ),
            "per_category": category_breakdown(
                fresh_predictions,
                fresh_resolutions,
                include_mock=include_mock,
                limit=limit,
            ),
            "label": PAPER_LABEL,
        }
        report["report_paths"] = (
            write_reports(report, out_dir=out_dir) if write else {"json": None, "markdown": None}
        )
        return report
    finally:
        if owns_gamma:
            try:
                gamma.close()
            except Exception:  # noqa: BLE001 - best effort
                pass


def render_markdown(report: dict) -> str:
    """Render the grading report as markdown (headline + bins + categories).

    Args:
        report: The ``grade_ledger`` report dict.

    Returns:
        Markdown text.
    """
    ledger = report["ledger"]
    lines = [
        "# Prediction ledger — automatic resolution grading",
        "",
        f"* as of: {report['as_of']}",
        f"* database: `{report['db']}`",
        f"* graded this run: {report['graded_now']}",
        f"* label: {report['label']}",
        "",
        "## Skips (never guessed)",
        "",
        "| reason | n |",
        "|---|---|",
    ]
    for key, count in report["skipped"].items():
        lines.append(f"| {key} | {count} |")
    lines += [
        "",
        "## Headline metrics",
        "",
        "| metric | value |",
        "|---|---|",
        f"| n_logged | {ledger['n_logged']} |",
        f"| n_resolved | {ledger['n_resolved']} |",
        f"| n_abstained | {ledger['n_abstained']} |",
        f"| abstention_rate | {ledger['abstention_rate']} |",
        f"| direction_accuracy (guessed) | {ledger['direction_accuracy']} |",
    ]
    brier = ledger["brier"]
    if brier:
        lines += [
            f"| brier model | {brier['model']} |",
            f"| brier market | {brier['market']} |",
            f"| brier skill vs market | {brier['skill_vs_market']} |",
            f"| brier skill vs 0.5 | {brier['skill_vs_baseline']} |",
        ]
    lines += [
        "",
        "## Calibration bins",
        "",
        "| bin | n | mean p | event rate |",
        "|---|---|---|---|",
    ]
    for row in ledger["bins"]:
        lines.append(
            f"| {row['lo']}–{row['hi']} | {row['n']} | {row['mean_p']} | "
            f"{row['event_rate']} |"
        )
    if report["per_category"]:
        lines += [
            "",
            "## Per category (market tags; a market counts in each of its tags)",
            "",
            "| tag | n_resolved | direction_accuracy | brier model |",
            "|---|---|---|---|",
        ]
        for row in report["per_category"]:
            model = (row["brier"] or {}).get("model")
            lines.append(
                f"| {row['tag']} | {row['n_resolved']} | "
                f"{row['direction_accuracy']} | {model} |"
            )
    return "\n".join(lines) + "\n"


def write_reports(report: dict, out_dir: str | Path | None = None) -> dict:
    """Write ``grade-<stamp>.json`` + ``.md`` into the playtest reports dir.

    Args:
        report: The ``grade_ledger`` report dict.
        out_dir: Destination dir (default ``backend/playtest_reports/`` — the
            one existing reports directory; no second one is created).

    Returns:
        ``{"json": path, "markdown": path}`` as strings.
    """
    root = (
        Path(out_dir)
        if out_dir
        else Path(__file__).resolve().parents[1] / "playtest_reports"
    )
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    json_path = root / f"grade-{stamp}.json"
    markdown_path = root / f"grade-{stamp}.md"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: grade the ledger, write reports, print a summary."""
    parser = argparse.ArgumentParser(
        description="Grade the prediction ledger against resolved markets "
        "(read-only Gamma; appends predict_resolved events; never a trade)"
    )
    parser.add_argument(
        "--db",
        default=None,
        help="audit DB path (default: DATABASE_PATH from Settings)",
    )
    parser.add_argument("--limit", type=int, default=5000, help="max predictions")
    parser.add_argument(
        "--include-mock",
        action="store_true",
        help="include mock predictions in the metrics",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="report directory (default: backend/playtest_reports)",
    )
    parser.add_argument(
        "--no-write", action="store_true", help="do not write report files"
    )
    args = parser.parse_args(argv)

    settings = Settings()
    db_path = args.db or str(resolve_db_path(settings.DATABASE_PATH))
    report = grade_ledger(
        db_path,
        limit=args.limit,
        include_mock=args.include_mock,
        write=not args.no_write,
        out_dir=args.out_dir,
    )
    print(
        json.dumps(
            {
                "db": report["db"],
                "graded_now": report["graded_now"],
                "skipped": report["skipped"],
                "n_resolved": report["ledger"]["n_resolved"],
                "direction_accuracy": report["ledger"]["direction_accuracy"],
                "report_paths": report["report_paths"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
