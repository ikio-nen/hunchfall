"""Jev (TypeSafe hosted) decision-model client.

VERIFIED: single typed endpoint::

    POST https://api.typesafe.ai/v1/systemone
    headers: Authorization: Bearer <JEV_API_KEY>, Content-Type: application/json
    body: {"state": <compact text>, "model": <pinned id>,
           "questions": {<caller-chosen id>: {...}}}

``model`` is PINNED to a versioned ID via ``JEV_MODEL``
(default ``"jev-1.13.0"``) — never ``"jev-latest"``. The client logs the
response's ``model`` field (``Decision.jev_model``) so the audit trail can
prove exactly which model version judged each signal.

One call carries three questions in parallel:

1. ``news_edge`` — ``{"type": "noul", "instructions": ..., "criteria":
   {"yes": ..., "no": ...}}`` -> ``{"noul": 0..1}``. NO confidence field;
   ``~0.5`` = uncertain. The raw noul value is used directly as P(true).
2. ``trade_action`` — ``{"type": "choice", "instructions": ...,
   "options": ["YES","NO","SKIP"] (with per-option rubrics)}`` ->
   ``{"choice", "probabilities", "confidence"}``. Option ORDER IS
   RANDOMIZED on every call (documented Jev position sensitivity: the model
   favors options by position, so the order is shuffled per call and the
   returned choice is normalized back to the canonical label). Vetoed
   (``model_skip``) when choice is SKIP.
3. ``signal_strength`` — ``{"type": "score", "criteria":
   ["noise","weak","medium","strong"]}`` -> a 4-level legend with
   probabilities + confidence. The client converts to a weighted float in
   [0, 1] (``signal_score``) using the legend.

Pricing (verified): $0.042 / 1M input tokens, output free.
Limits: 1200 req/min, 250k tokens/sec, 32k state+longest-question,
64k total. Latency ~250ms p50.

MOCK mode (default, ``JEV_MOCK=true`` or empty ``JEV_API_KEY``): returns a
deterministic seeded pseudo-decision derived from hash(state). Labeled
``mock=True`` and documented "MOCK — not a real model". This is for
offline pipeline testing only; it must never be reported as a model
judgment or used to claim win rates.
"""

from __future__ import annotations

import hashlib
import logging
import random
from dataclasses import dataclass, field

import httpx

from app.config import Settings

log = logging.getLogger(__name__)

_PATH = "/v1/systemone"

# Signal-strength legend: 4 ordered levels -> weighted-float weights.
_SCORE_WEIGHTS = {"noise": 0.0, "weak": 0.33, "medium": 0.67, "strong": 1.0}


@dataclass
class DecisionState:
    """Compact state handed to the decision model.

    Fields:
        news_summary: Extractive summary of the triggering story.
        market_question: Polymarket market question text.
        yes_price: Live CLOB YES price (0..1).
        extra_context: Free-form extra context (rules snippet, volume, ...).
    """

    news_summary: str
    market_question: str
    yes_price: float
    extra_context: str = ""


@dataclass
class Decision:
    """One typed model decision.

    Fields:
        p_true: P(true) = raw noul from the ``news_edge`` question.
        choice: Canonical "YES" | "NO" | "SKIP" from ``trade_action``.
        signal_strength: Strongest 4-level label ("noise".."strong").
        signal_score: Weighted float in [0, 1] over the score legend.
        jev_model: Model version from the response ``model`` field
            (audit must record this; e.g. "jev-1.13.0").
        jev_confidence: Confidence from the ``trade_action`` response.
        jev_choice: Raw choice label from the model (== choice, kept for
            the audit trail).
        raw: Raw response payload from Jev (or the mock seed record).
        mock: True when this came from the MOCK path — not a real model.
    """

    p_true: float
    choice: str
    signal_strength: str
    signal_score: float = 0.0
    jev_model: str = ""
    jev_confidence: float = 0.0
    jev_choice: str = ""
    raw: dict = field(default_factory=dict)
    mock: bool = False


def build_state_text(state: DecisionState) -> str:
    """Build the compact state text sent to Jev (news + question + YES price).

    Args:
        state: DecisionState.

    Returns:
        Compact multi-line text for the model context.
    """
    lines = [
        f"News: {state.news_summary}",
        f"Market: {state.market_question}",
        f"YES price: {state.yes_price:.3f}",
    ]
    if state.extra_context:
        lines.append(f"Context: {state.extra_context}")
    return "\n".join(lines)


def _questions(option_order: list[str]) -> dict:
    """Build the three-question payload (option order pre-shuffled).

    The ``/v1/systemone`` dialect is validated strictly by laya-serve
    (live-checked 2026-10-02): noul criteria are keyed ``true``/``false``
    and a choice question carries ``criteria`` as a label -> description
    map. The earlier ``yes``/``no`` keys and ``options``/``rubric`` list
    were rejected with HTTP 422 ("takes 'criteria' keyed only
    'true'/'false'").

    Args:
        option_order: Shuffled canonical option labels for trade_action.

    Returns:
        Dict keyed by caller-chosen question ids.
    """
    return {
        "news_edge": {
            "type": "noul",
            "instructions": (
                "Does the news make the market's YES outcome more likely? "
                "Return noul near 1 when YES is more likely, near 0 when NO "
                "is more likely, ~0.5 when the news says nothing either way."
            ),
            "criteria": {
                "true": "The news raises the probability of the YES outcome.",
                "false": "The news lowers the probability of the YES outcome.",
            },
        },
        "trade_action": {
            "type": "choice",
            "instructions": (
                "Given the news and the current YES price, what should a "
                "paper trader do? Choose SKIP when the news is ambiguous, "
                "already priced in, or unrelated to the market."
            ),
            # NOTE: order is randomized per call (see JevClient.decide) to
            # defeat Jev's documented option-position sensitivity; the
            # caller normalizes the returned label back to canonical form.
            "criteria": {
                label: {
                    "YES": "News supports YES and the price underreacts.",
                    "NO": "News undermines YES and the price underreacts.",
                    "SKIP": "Ambiguous, priced in, or unrelated news.",
                }[label]
                for label in option_order
            },
        },
        "signal_strength": {
            "type": "score",
            "instructions": (
                "Rate how strongly the news moves the market's true "
                "probability, as one of: noise, weak, medium, strong."
            ),
            "criteria": ["noise", "weak", "medium", "strong"],
        },
    }


class JevClient:
    """Client for the TypeSafe Jev decision API (one call, three questions)."""

    def __init__(self, settings: Settings) -> None:
        """Store settings; mock when JEV_MOCK or no JEV_API_KEY.

        Args:
            settings: App Settings.
        """
        self.settings = settings
        self._mock = bool(settings.JEV_MOCK) or not settings.JEV_API_KEY.strip()
        self._model = (settings.JEV_MODEL or "jev-1.13.0").strip() or "jev-1.13.0"
        self._http: httpx.Client | None = None
        if not self._mock:
            self._http = httpx.Client(
                base_url=settings.JEV_API_URL.rstrip("/"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {settings.JEV_API_KEY}",
                },
                timeout=30.0,
            )

    @property
    def is_mock(self) -> bool:
        """True when decisions come from the MOCK path."""
        return self._mock

    # ---- MOCK path ------------------------------------------------------
    def _mock_decision(self, state: DecisionState) -> Decision:
        """Deterministic seeded pseudo-decision. MOCK — not a real model.

        Seeded from sha256 of the state text so the same state always
        yields the same mock decision (useful for pipeline tests).

        Args:
            state: DecisionState.

        Returns:
            Decision with mock=True, clearly labeled as a mock.
        """
        seed = int(hashlib.sha256(build_state_text(state).encode()).hexdigest(), 16)
        rng = random.Random(seed)
        p_true = round(rng.uniform(0.05, 0.95), 4)
        # lean toward the price side, like a weak informed prior
        tilt = 0.5 + (state.yes_price - 0.5) * 0.4
        p_true = round(min(0.95, max(0.05, p_true * 0.5 + tilt * 0.5)), 4)
        choice = "YES" if p_true > 0.6 else ("NO" if p_true < 0.4 else "SKIP")
        spread = abs(p_true - state.yes_price)
        strength = (
            "strong" if spread > 0.25 else ("medium" if spread > 0.12 else "weak")
        )
        confidence = round(rng.uniform(0.55, 0.95), 4)
        return Decision(
            p_true=p_true,
            choice=choice,
            signal_strength=strength,
            signal_score=round(_SCORE_WEIGHTS[strength], 4),
            jev_model="jev-mock",
            jev_confidence=confidence,
            jev_choice=choice,
            raw={"mock": True, "seed": seed, "note": "MOCK — not a real model"},
            mock=True,
        )

    # ---- live path ------------------------------------------------------
    def _post(self, payload: dict) -> dict:
        """POST the one-call body to /v1/systemone; raises RuntimeError.

        Args:
            payload: Full JSON body (state, model, questions).

        Raises:
            RuntimeError: on HTTP failure, naming the endpoint.
        """
        endpoint = f"{self.settings.JEV_API_URL.rstrip('/')}{_PATH}"
        assert self._http is not None
        try:
            resp = self._http.post(_PATH, json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Jev API request failed [{endpoint}]: {exc}") from exc
        return resp.json()

    @staticmethod
    def _signal_score(payload: dict) -> tuple[str, float]:
        """Normalize a score response to (strongest label, weighted float).

        Accepts either a numeric ``score`` in [0,1], or a ``probabilities``
        map over the 4-level legend.

        Args:
            payload: The ``signal_strength`` question response.

        Returns:
            (label, weighted score in [0, 1]).
        """
        raw_score = payload.get("score", payload.get("value"))
        if isinstance(raw_score, (int, float)):
            s = max(0.0, min(1.0, float(raw_score)))
            label = max(
                _SCORE_WEIGHTS, key=lambda k: -abs(_SCORE_WEIGHTS[k] - s)
            )
            return label, round(s, 4)
        probs = payload.get("probabilities") or {}
        weighted = 0.0
        best_label, best_p = "noise", -1.0
        for label, weight in _SCORE_WEIGHTS.items():
            p = float(probs.get(label, 0.0) or 0.0)
            weighted += p * weight
            if p > best_p:
                best_label, best_p = label, p
        return best_label, round(max(0.0, min(1.0, weighted)), 4)

    def decide(self, state: DecisionState) -> Decision:
        """Run one call with three parallel questions; return a Decision.

        In MOCK mode returns the deterministic seeded pseudo-decision
        (mock=True). Live path: ``POST {JEV_API_URL}/v1/systemone`` with::

            {"state": <compact text>, "model": <JEV_MODEL pinned>,
             "questions": {"news_edge": {...}, "trade_action": {...},
                           "signal_strength": {...}}}

        The ``trade_action`` option order is shuffled on every call to
        defeat Jev's documented position sensitivity; the returned label
        is normalized to the canonical YES/NO/SKIP.

        Args:
            state: DecisionState.

        Returns:
            Decision (mock=True when MOCK mode).
        """
        if self._mock:
            log.debug("jev: MOCK decision (not a real model)")
            return self._mock_decision(state)

        context = build_state_text(state)
        # Randomize option order across calls (position sensitivity).
        option_order = ["YES", "NO", "SKIP"]
        random.shuffle(option_order)

        resp = self._post(
            {
                "state": context,
                "model": self._model,
                "questions": _questions(option_order),
            }
        )
        results = resp.get("results", resp.get("answers", resp.get("questions", {})))

        # 1) noul: raw value is P(true); ~0.5 = uncertain. No confidence field.
        noul_q = results.get("news_edge", {})
        p_true = float(noul_q.get("noul", noul_q.get("p_true", 0.5)))
        p_true = max(0.0, min(1.0, p_true))

        # 2) choice: normalize the label (order was shuffled).
        choice_q = results.get("trade_action", {})
        raw_choice = str(choice_q.get("choice", "SKIP")).strip().upper()
        choice = raw_choice if raw_choice in ("YES", "NO", "SKIP") else "SKIP"
        confidence = float(choice_q.get("confidence", 0.5) or 0.5)

        # 3) score: 4-level legend -> weighted float.
        score_q = results.get("signal_strength", {})
        strength, score = self._signal_score(score_q)

        return Decision(
            p_true=round(p_true, 4),
            choice=choice,
            signal_strength=strength,
            signal_score=score,
            jev_model=str(resp.get("model", self._model)),
            jev_confidence=round(max(0.0, min(1.0, confidence)), 4),
            jev_choice=choice,
            raw=resp,
            mock=False,
        )

    def close(self) -> None:
        """Close the underlying httpx client (live mode only)."""
        if self._http is not None:
            self._http.close()
