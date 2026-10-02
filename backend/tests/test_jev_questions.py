"""Pin the ``/v1/systemone`` question dialect laya-serve validates strictly.

Live-checked 2026-10-02: the local Laya server rejected the original payload
with HTTP 422 — noul ``criteria`` keyed ``yes``/``no`` (must be
``true``/``false``) and a choice question built from ``options``/``rubric``
(the server takes ``criteria`` as a label -> description map). Both are
pinned here so the client cannot drift back to a shape the model rejects.
"""

from __future__ import annotations

from app.jev.client import _questions


def test_noul_criteria_uses_true_false_keys():
    questions = _questions(["YES", "NO", "SKIP"])
    assert set(questions["news_edge"]["criteria"]) == {"true", "false"}


def test_choice_question_uses_criteria_not_options():
    questions = _questions(["YES", "NO", "SKIP"])
    action = questions["trade_action"]
    assert "options" not in action
    assert set(action["criteria"]) == {"YES", "NO", "SKIP"}
    assert all(
        isinstance(description, str) and description
        for description in action["criteria"].values()
    )


def test_shuffled_option_order_is_preserved_in_choice_criteria():
    questions = _questions(["SKIP", "NO", "YES"])
    assert list(questions["trade_action"]["criteria"]) == ["SKIP", "NO", "YES"]
