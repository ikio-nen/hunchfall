"""Labeled-example schema for playtesting.

A labeled example pairs a news snapshot and an odds snapshot with the known
outcome, so a decision function can be scored honestly. ``label`` is
``"SAMPLE"`` for synthetic hand-written cases and ``"REAL"`` for genuinely
labeled historical data — the two must never be mixed without saying so.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class LabeledExample:
    """One scored playtesting case."""

    news_snapshot: str  # news text available at decision time
    market_question: str  # market question text
    odds_snapshot_yes: float  # YES price at decision time, in [0, 1]
    outcome: str  # "YES" or "NO" — the known resolution
    source: str  # e.g. "synthetic-hand-written"
    label: str  # "SAMPLE" or "REAL"

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "LabeledExample":
        """Build from a dict (extra keys are ignored).

        Args:
            data: Dict with the dataclass fields.

        Returns:
            LabeledExample.
        """
        fields = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in fields})


def load_jsonl(path: str | Path) -> list[LabeledExample]:
    """Load labeled examples from a JSONL file (one JSON object per line).

    Args:
        path: Path to the .jsonl file.

    Returns:
        List of LabeledExample.
    """
    examples: list[LabeledExample] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                examples.append(LabeledExample.from_dict(json.loads(line)))
    return examples


def save_jsonl(examples: list[LabeledExample], path: str | Path) -> str:
    """Save labeled examples to a JSONL file.

    Args:
        examples: Examples to save.
        path: Destination path.

    Returns:
        The path as a string.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        for ex in examples:
            f.write(json.dumps(ex.to_dict(), ensure_ascii=False) + "\n")
    return str(p)
