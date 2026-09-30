"""RFC-003 calibration ledger — hand-computed, derived on read."""

from __future__ import annotations

from app.predict import accuracy_report


def _predict(
    row_id: int,
    *,
    p_yes: float,
    market: float,
    abstained: bool = False,
    mock: bool = False,
    pid: str | None = None,
) -> dict:
    return {
        "id": row_id,
        "ts": "2026-09-30T00:00:00Z",
        "event_type": "predict",
        "payload": {
            "prediction_id": pid or f"p{row_id}",
            "p_yes": p_yes,
            "market_price": market,
            "edge_vs_market": round(p_yes - market, 4),
            "abstained": abstained,
            "model": {"mock": mock},
        },
    }


def _resolved(row_id: int, pid: str, outcome: str) -> dict:
    return {
        "id": row_id,
        "ts": "2026-09-30T01:00:00Z",
        "event_type": "predict_resolved",
        "payload": {"prediction_id": pid, "outcome": outcome},
    }


def test_empty_ledger_is_honest_zeros():
    report = accuracy_report([], [])
    assert report["n_logged"] == 0
    assert report["n_resolved"] == 0
    assert report["abstention_rate"] is None
    assert report["brier"] is None
    assert report["direction_accuracy"] is None
    assert len(report["bins"]) == 10
    assert all(bin_["n"] == 0 and bin_["mean_p"] is None for bin_ in report["bins"])


def test_brier_skill_bins_and_direction_accuracy():
    predictions = [
        _predict(1, p_yes=0.70, market=0.60),
        _predict(2, p_yes=0.30, market=0.40),
        _predict(3, p_yes=0.50, market=0.50, abstained=True),
    ]
    resolutions = [_resolved(10, "p1", "YES"), _resolved(11, "p2", "NO")]
    report = accuracy_report(predictions, resolutions)

    assert report["n_logged"] == 3
    assert report["n_resolved"] == 2
    assert report["n_abstained"] == 1
    assert report["n_guessed"] == 2
    assert report["n_resolved_guessed"] == 2
    assert report["abstention_rate"] == 0.3333
    assert report["mean_abs_edge"] == 0.1

    brier = report["brier"]
    # model = mean((0.7-1)^2, (0.3-0)^2) = 0.09 ; market = mean(0.16, 0.16) = 0.16
    assert brier["model"] == 0.09
    assert brier["market"] == 0.16
    assert brier["baseline_0_5"] == 0.25
    assert brier["skill_vs_market"] == 0.4375  # 1 - 0.09/0.16
    assert brier["skill_vs_baseline"] == 0.64  # 1 - 0.09/0.25

    assert report["brier_guessed"]["n"] == 2
    assert report["direction_accuracy"] == 1.0

    populated = {bin_["mean_p"]: bin_ for bin_ in report["bins"] if bin_["n"]}
    assert populated[0.7]["event_rate"] == 1.0
    assert populated[0.3]["event_rate"] == 0.0


def test_mock_split_and_include_mock_scoping():
    predictions = [
        _predict(1, p_yes=0.70, market=0.60),
        _predict(2, p_yes=0.30, market=0.40),
        _predict(3, p_yes=0.50, market=0.50, abstained=True),
        _predict(4, p_yes=0.90, market=0.50, mock=True),
    ]
    resolutions = [
        _resolved(10, "p1", "YES"),
        _resolved(11, "p2", "NO"),
        _resolved(12, "p4", "YES"),
    ]

    excluded = accuracy_report(predictions, resolutions, include_mock=False)
    assert excluded["n_logged"] == 3  # the mock prediction is excluded
    assert excluded["mock_split"]["mock"] == {"n_logged": 1, "n_resolved": 1}
    assert excluded["mock_split"]["live"] == {"n_logged": 3, "n_resolved": 2}

    included = accuracy_report(predictions, resolutions, include_mock=True)
    assert included["n_logged"] == 4
    assert included["n_resolved"] == 3


def test_duplicate_resolutions_use_the_newest():
    predictions = [_predict(1, p_yes=0.70, market=0.60)]
    resolutions = [
        _resolved(10, "p1", "NO"),
        _resolved(11, "p1", "YES"),  # newer row wins
    ]
    report = accuracy_report(predictions, resolutions)
    assert report["n_resolved"] == 1
    # outcome YES -> (0.7-1)^2 = 0.09, not (0.7-0)^2
    assert report["brier"]["model"] == 0.09


def test_unresolved_predictions_are_not_scored():
    predictions = [_predict(1, p_yes=0.9, market=0.1)]
    report = accuracy_report(predictions, [])
    assert report["n_logged"] == 1
    assert report["n_resolved"] == 0
    assert report["brier"] is None
