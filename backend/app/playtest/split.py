"""Train/test splitting for playtesting, stratified by outcome."""
from __future__ import annotations

import random

from app.playtest.schema import LabeledExample


def train_test_split(
    examples: list[LabeledExample],
    test_frac: float = 0.3,
    seed: int = 42,
) -> tuple[list[LabeledExample], list[LabeledExample]]:
    """Split examples into tuning and test sets, stratified by outcome.

    Each outcome group ("YES"/"NO"/...) is shuffled with the seeded RNG and
    split independently, so both sets keep roughly the outcome mix of the
    full set.

    Args:
        examples: Labeled examples to split.
        test_frac: Fraction of each outcome group assigned to the test set.
        seed: RNG seed for reproducibility.

    Returns:
        ``(tuning, test)`` lists.
    """
    rng = random.Random(seed)
    groups: dict[str, list[LabeledExample]] = {}
    for ex in examples:
        groups.setdefault(ex.outcome, []).append(ex)
    tuning: list[LabeledExample] = []
    test: list[LabeledExample] = []
    for outcome in sorted(groups):
        group = list(groups[outcome])
        rng.shuffle(group)
        n_test = round(len(group) * test_frac)
        test.extend(group[:n_test])
        tuning.extend(group[n_test:])
    return tuning, test
