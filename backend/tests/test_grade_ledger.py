"""Automatic resolution grading: confirm-or-skip, idempotence, metrics.

All Gamma reads are an offline fake, so the suite is zero-network; the
metrics fixture is hand-computed in the comments.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.memory.audit import AuditLog
from app.paths import resolve_db_path
from app.polymarket.gamma import GammaError
from scripts import grade_ledger
from tests.fakes import make_settings

COND_YES = "0x" + "a" * 64
COND_NO = "0x" + "b" * 64
COND_ZEROED = "0x" + "c" * 64
COND_OPEN = "0x" + "d" * 64
COND_UNKNOWN = "0x" + "e" * 64


def _market(
    *, closed=True, prices='["1","0"]', outcomes='["Yes","No"]', tags=None
) -> dict:
    return {
        "closed": closed,
        "outcomePrices": prices,
        "outcomes": outcomes,
        "tags": tags or [],
    }


class FakeGamma:
    """Offline Gamma: condition id -> canned market payload."""

    def __init__(self, markets: dict | None = None) -> None:
        self.markets = markets or {}
        self.calls: list[str] = []

    def get_market_by_condition_id(self, condition_id: str) -> dict:
        self.calls.append(condition_id)
        return dict(self.markets.get(condition_id) or {})

    def close(self) -> None:
        pass


class ExplodingGamma:
    """Gamma that is down for every lookup."""

    def get_market_by_condition_id(self, condition_id: str) -> dict:
        raise GammaError(f"gamma down for {condition_id}")

    def close(self) -> None:
        pass


def _gamma() -> FakeGamma:
    return FakeGamma(
        {
            COND_YES: _market(tags=[{"label": "Crypto"}]),
            COND_NO: _market(prices='["0","1"]', tags=[{"label": "Sports"}]),
            COND_ZEROED: _market(prices='["0","0"]'),  # documented gotcha
            COND_OPEN: _market(closed=False, prices='["0.5","0.5"]'),
            # COND_UNKNOWN intentionally absent -> not_found
        }
    )


def _predict_payload(
    pid: str,
    *,
    p_yes: float,
    market: float,
    direction: str,
    abstained: bool = False,
    condition: str = COND_YES,
) -> dict:
    return {
        "prediction_id": pid,
        "condition_id": condition,
        "p_yes": p_yes,
        "market_price": market,
        "edge_vs_market": round(p_yes - market, 4),
        "direction": direction,
        "confidence": 0.5,
        "abstained": abstained,
        "model": {"mock": False, "mode": "live"},
        "snapshot_sha256": "deadbeef",
        "reasons": ["r"],
    }


def _seeded_db(tmp_path) -> tuple[AuditLog, Path]:
    """p1 YES-correct · p2 NO-correct · p3 abstained · p4 zeroed · p5 open ·
    p6 unknown · p7 no condition · p8 manually settled."""
    settings = make_settings(tmp_path)
    audit = AuditLog(resolve_db_path(settings.DATABASE_PATH))
    audit.record("predict", _predict_payload("p1", p_yes=0.8, market=0.6, direction="YES"))
    audit.record(
        "predict",
        _predict_payload("p2", p_yes=0.2, market=0.4, direction="NO", condition=COND_NO),
    )
    audit.record(
        "predict",
        _predict_payload(
            "p3", p_yes=0.5, market=0.5, direction="ABSTAIN", abstained=True
        ),
    )
    audit.record(
        "predict",
        _predict_payload("p4", p_yes=0.6, market=0.5, direction="YES", condition=COND_ZEROED),
    )
    audit.record(
        "predict",
        _predict_payload("p5", p_yes=0.6, market=0.5, direction="YES", condition=COND_OPEN),
    )
    audit.record(
        "predict",
        _predict_payload("p6", p_yes=0.6, market=0.5, direction="YES", condition=COND_UNKNOWN),
    )
    audit.record(
        "predict",
        _predict_payload("p7", p_yes=0.6, market=0.5, direction="YES", condition=""),
    )
    audit.record("predict", _predict_payload("p8", p_yes=0.8, market=0.6, direction="YES"))
    audit.record(
        "predict_resolved",
        {"prediction_id": "p8", "outcome": "YES", "resolved_at": "x", "source": "manual"},
    )
    return audit, resolve_db_path(settings.DATABASE_PATH)


# -------------------------------------------------------------------- grading
def test_grading_confirms_winners_and_skips_everything_unconfirmed(tmp_path):
    audit, db = _seeded_db(tmp_path)
    report = grade_ledger.grade_ledger(db, gamma=_gamma(), write=False)

    assert report["graded_now"] == 3
    by_pid = {
        row["payload"]["prediction_id"]: row["payload"]
        for row in audit.prediction_resolutions()
    }
    # only confirmed winners were graded; the manual verdict is untouched
    assert set(by_pid) == {"p1", "p2", "p3", "p8"}
    assert by_pid["p1"]["outcome"] == "YES"
    assert by_pid["p2"]["outcome"] == "NO"
    assert by_pid["p3"]["outcome"] == "YES"
    assert by_pid["p1"]["source"] == "gamma"
    assert by_pid["p1"]["tags"] == ["Crypto"]
    assert by_pid["p8"]["source"] == "manual"

    assert report["skipped"] == {
        "already_graded": 1,
        "no_condition_id": 1,
        "unresolved": 1,
        "not_found": 1,
        "ambiguous": 1,
        "errors": 0,
    }


def test_metrics_are_hand_computed_from_the_graded_ledger(tmp_path):
    _audit, db = _seeded_db(tmp_path)
    report = grade_ledger.grade_ledger(db, gamma=_gamma(), write=False)
    ledger = report["ledger"]

    assert ledger["n_logged"] == 8
    # p1/p2/p3 graded this run + p8, whose verdict predates the run
    assert ledger["n_resolved"] == 4
    assert ledger["n_abstained"] == 1
    assert ledger["abstention_rate"] == 0.125
    # scored: p1 0.04 ; p2 0.04 ; p3 0.25 ; p8 (manual YES) 0.04 -> 0.37/4
    assert ledger["brier"]["model"] == 0.0925
    # market: 0.16 + 0.16 + 0.25 + 0.16 = 0.73 over 4
    assert ledger["brier"]["market"] == 0.1825
    assert ledger["brier"]["skill_vs_market"] == 0.4932  # 1 - 0.0925/0.1825
    assert ledger["brier"]["skill_vs_baseline"] == 0.63  # 1 - 0.0925/0.25
    assert ledger["n_resolved_guessed"] == 3
    assert ledger["direction_accuracy"] == 1.0

    bins = {row["lo"]: row for row in ledger["bins"]}
    assert bins[0.8]["n"] == 2  # p1 + the manual p8
    assert bins[0.8]["mean_p"] == 0.8
    assert bins[0.8]["event_rate"] == 1.0
    assert bins[0.2]["event_rate"] == 0.0
    assert bins[0.5]["event_rate"] == 1.0

    categories = {row["tag"]: row for row in report["per_category"]}
    assert set(categories) == {"Crypto", "Sports"}
    assert categories["Crypto"]["n_resolved"] == 2
    assert categories["Crypto"]["brier"]["model"] == 0.145  # (0.04 + 0.25)/2
    assert categories["Crypto"]["direction_accuracy"] == 1.0
    assert categories["Sports"]["n_resolved"] == 1
    assert categories["Sports"]["brier"]["model"] == 0.04


def test_rerun_is_idempotent_and_never_double_counts(tmp_path):
    audit, db = _seeded_db(tmp_path)
    gamma = _gamma()
    first = grade_ledger.grade_ledger(db, gamma=gamma, write=False)
    second = grade_ledger.grade_ledger(db, gamma=gamma, write=False)

    assert first["graded_now"] == 3
    assert second["graded_now"] == 0
    assert second["skipped"]["already_graded"] == 4  # 3 gamma + 1 manual
    assert len(audit.prediction_resolutions()) == 4  # no duplicate verdicts
    assert second["ledger"]["n_resolved"] == first["ledger"]["n_resolved"]
    assert second["ledger"]["brier"] == first["ledger"]["brier"]


def test_a_gamma_failure_is_recorded_and_never_fatal(tmp_path):
    audit, db = _seeded_db(tmp_path)
    report = grade_ledger.grade_ledger(db, gamma=ExplodingGamma(), write=False)

    assert report["graded_now"] == 0
    # p7 has no condition id and p8 is already settled: 6 lookups that failed
    assert report["skipped"]["errors"] == 6
    assert [row["event_type"] for row in audit.prediction_resolutions()] == [
        "predict_resolved"
    ]
    stages = {row["payload"]["stage"] for row in audit.query("stage_error")}
    assert stages == {"grade_ledger.gamma"}


# -------------------------------------------------------------------- reports
def test_writes_json_and_markdown_into_the_reports_dir(tmp_path):
    _audit, db = _seeded_db(tmp_path)
    out = tmp_path / "reports"
    report = grade_ledger.grade_ledger(db, gamma=_gamma(), out_dir=out)

    json_path = Path(report["report_paths"]["json"])
    markdown_path = Path(report["report_paths"]["markdown"])
    assert json_path.parent == out
    assert markdown_path.parent == out
    assert json_path.name.startswith("grade-")
    assert markdown_path.suffix == ".md"

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["graded_now"] == 3
    assert payload["ledger"]["brier"]["model"] == 0.0925
    assert "report_paths" not in payload  # written before the paths are attached

    text = markdown_path.read_text(encoding="utf-8")
    assert "automatic resolution grading" in text
    assert "| brier model | 0.0925 |" in text
    assert "Crypto" in text
