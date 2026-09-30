"""Extension-scan service: re-validate a page hint, record a paper hunch.

PAPER TRADING ONLY — prediction only. ``PaperEngine`` is deliberately NOT
imported here: an approved scan records the signal + gate outcome and never
places (even simulated) a fill.

Pipeline::

    slug -> Gamma market/event (official)
         -> CLOB book (live mid/spread/depth/book levels; authoritative)
         -> Data API v2 tape (/v2/activity?type=TRADE&condition=...)
         -> Jev typed decision (mock when JEV_MOCK=true)
         -> deterministic RiskGate
         -> append-only audit events (scan + signal + vetoes)

The scraped ``page_state`` is untrusted: its prices are ignored, the payload
is hashed and redacted (never stored raw), and only a sanitized title may
reach the model context. Client-supplied timestamps are drift-checked but
never used as truth — the server clock anchors every signal.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.config import Settings
from app.jev.client import DecisionState, JevClient
from app.loop import (
    BOOK_DEPTH_LEVELS,
    _exposure_usd,
    _fee_category,
    _hours_to_resolution,
    _load_state,
    _market_tokens,
    _uma_dispute,
)
from app.memory.audit import AuditLog
from app.paths import utcnow_iso
from app.policy.gate import RiskGate, Signal, buy_direction
from app.polymarket.clob import ClobClient
from app.polymarket.data_api import DataApiClient, DataApiError
from app.polymarket.gamma import GammaClient, GammaError, is_resolved, negrisk_sum

#: page_state keys that are recorded as ignored (scraped, never trusted).
PAGE_STATE_PRICE_KEYS = ("yesPrice", "noPrice")
#: Cap on the sanitized page title that may inform the model context.
TITLE_MAX_CHARS = 300
#: Max trade-tape items pulled from Data API v2.
TAPE_LIMIT = 50
#: The badge label every prediction surface must carry.
PAPER_LABEL = "paper prediction · no trade placed"


class ScanError(Exception):
    """Base scan error carrying the HTTP status and machine code."""

    status = 500
    code = "scan_error"

    def __init__(self, message: str, *, source: str | None = None) -> None:
        """Store the message and optional upstream source name."""
        super().__init__(message)
        self.message = message
        self.source = source


class ScanBadRequest(ScanError):
    """Malformed scan request (400)."""

    status = 400
    code = "bad_request"


class ScanClockSkew(ScanBadRequest):
    """Client clock drifted too far from the server (400)."""

    code = "clock_skew"


class ScanNotFound(ScanError):
    """Gamma has no market/event for the slug (404)."""

    status = 404
    code = "market_not_found"


class ScanNotTradeable(ScanError):
    """Closed/resolved market or missing token pair (409)."""

    status = 409
    code = "market_not_tradeable"


class ScanUpstreamError(ScanError):
    """Gamma/CLOB/Data API/Jev failure — no signal persisted (502)."""

    status = 502
    code = "upstream_error"


def _iso_to_dt(raw: str) -> datetime | None:
    """Parse an ISO-8601 timestamp (None when unparseable)."""
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _gamma_404(exc: Exception) -> bool:
    """Best-effort 404 detection for a GammaError message."""
    return "404" in str(exc)


def _first(item: dict, keys: tuple[str, ...], default: Any = None) -> Any:
    """First present, non-None value among ``keys``."""
    for key in keys:
        value = item.get(key)
        if value is not None:
            return value
    return default


def _float(value: Any) -> float | None:
    """Coerce to float; None when not numeric."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _epoch_seconds(value: Any) -> float | None:
    """Best-effort epoch seconds from an epoch or ISO timestamp value."""
    numeric = _float(value)
    if numeric is not None:
        return numeric / 1000.0 if numeric > 1e12 else numeric
    if not value:
        return None
    dt = _iso_to_dt(str(value))
    return dt.timestamp() if dt is not None else None


class ScanService:
    """Re-validates an extension page hint and persists a paper hunch."""

    def __init__(self, settings: Settings, audit: AuditLog) -> None:
        """Store settings and the audit log.

        Args:
            settings: App Settings.
            audit: Append-only audit log.
        """
        self.settings = settings
        self.audit = audit

    # ---- request hygiene ---------------------------------------------------
    def _clock_skew(self, scanned_at: str) -> float:
        """Validate the client timestamp and return the drift in seconds."""
        parsed = _iso_to_dt(scanned_at)
        if parsed is None:
            raise ScanBadRequest("scanned_at must be an ISO-8601 timestamp")
        skew = abs((datetime.now(timezone.utc) - parsed).total_seconds())
        if skew > float(self.settings.SCAN_CLOCK_SKEW_SEC):
            raise ScanClockSkew(
                f"scanner clock drift {skew:.0f}s exceeds "
                f"SCAN_CLOCK_SKEW_SEC {self.settings.SCAN_CLOCK_SKEW_SEC}s"
            )
        return skew

    def _page_state_meta(
        self, page_state: Any
    ) -> tuple[str | None, list[str], str | None]:
        """Hash/redact the untrusted page_state and extract a safe title."""
        if not isinstance(page_state, dict):
            return None, [], None
        canonical = json.dumps(page_state, sort_keys=True, default=str)
        if len(canonical.encode("utf-8")) > int(self.settings.SCAN_PAGE_STATE_MAX_BYTES):
            raise ScanBadRequest("page_state exceeds the size cap")
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        ignored = [key for key in PAGE_STATE_PRICE_KEYS if key in page_state]
        title = page_state.get("title")
        clean: str | None = None
        if isinstance(title, str):
            clean = "".join(ch for ch in title if ch.isprintable()).strip()
            clean = clean[:TITLE_MAX_CHARS] or None
        return digest, ignored, clean

    # ---- market resolution -------------------------------------------------
    def _resolve_market(self, gamma: GammaClient, kind: str, slug: str) -> dict:
        """Resolve slug -> tradeable market, or raise a mapped error."""
        try:
            if kind == "market":
                market = gamma.get_market_by_slug(slug)
                event_slug = ""
            else:
                event = gamma.get_event_by_slug(slug)
                if not isinstance(event, dict) or not event:
                    raise ScanNotFound(f"Gamma has no event for slug {slug!r}")
                event_slug = str(event.get("slug") or slug)
                markets = [
                    m for m in (event.get("markets") or []) if isinstance(m, dict)
                ]
                market = next((m for m in markets if _market_tokens(m)), None)
                if market is None:
                    raise ScanNotFound(f"event {slug!r} has no tradeable market")
        except GammaError as exc:
            if _gamma_404(exc):
                raise ScanNotFound(f"Gamma has no market for slug {slug!r}") from exc
            self.audit.record(
                "stage_error", {"stage": "gamma.resolve", "error": str(exc)}
            )
            raise ScanUpstreamError(
                f"Gamma lookup failed: {exc}", source="gamma"
            ) from exc

        if not isinstance(market, dict) or not market.get("question"):
            raise ScanNotFound(f"Gamma has no market for slug {slug!r}")
        if market.get("closed") or is_resolved(market):
            raise ScanNotTradeable(f"market {slug!r} is closed/resolved")
        tokens = _market_tokens(market)
        if tokens is None:
            raise ScanNotTradeable(f"market {slug!r} has no two-sided token pair")
        condition_id = str(
            market.get("conditionId")
            or market.get("condition_id")
            or market.get("id")
            or ""
        )
        if not condition_id:
            raise ScanNotTradeable(f"market {slug!r} has no condition id")
        return {
            "market": market,
            "tokens": tokens,
            "condition_id": condition_id,
            "event_slug": event_slug,
        }

    # ---- live re-validation ------------------------------------------------
    def _book_snapshot(self, clob: ClobClient, token_id: str) -> dict | None:
        """Live book snapshot (mid/spread/depth/levels); None on failure.

        Uses the same math as the loop's snapshot: mid of the touch, spread
        in cents, top-of-book USD depth, top-N levels for the book-walk.
        """
        try:
            book = clob.get_orderbook(token_id) or {}
            bids = list(book.get("bids") or [])[:BOOK_DEPTH_LEVELS]
            asks = list(book.get("asks") or [])[:BOOK_DEPTH_LEVELS]
            if not bids or not asks:
                raise ValueError("empty book side")
            best_bid_p, best_bid_s = float(bids[0][0]), float(bids[0][1])
            best_ask_p, best_ask_s = float(asks[0][0]), float(asks[0][1])
            if not (0.0 < best_bid_p < 1.0 and 0.0 < best_ask_p < 1.0):
                raise ValueError("book price outside (0, 1)")
        except Exception as exc:  # noqa: BLE001 - recorded, mapped to 502
            self.audit.record(
                "stage_error", {"stage": "clob.snapshot", "error": str(exc)}
            )
            return None
        return {
            "mid_price": (best_bid_p + best_ask_p) / 2.0,
            "spread_cents": (best_ask_p - best_bid_p) * 100.0,
            "top_book_depth_usd": best_bid_p * best_bid_s + best_ask_p * best_ask_s,
            "book_asks": asks,
            "book_bids": bids,
            "price_ts": utcnow_iso(),
        }

    def _tape(self, data_api: DataApiClient, condition_id: str) -> dict:
        """Recent TRADE activity from Data API v2 (verified params)."""
        try:
            items = (
                data_api.get_activity_v2(
                    condition=condition_id, activity_type="TRADE", limit=TAPE_LIMIT
                )
                or []
            )
        except DataApiError as exc:
            self.audit.record(
                "stage_error", {"stage": "data-api.activity", "error": str(exc)}
            )
            raise ScanUpstreamError(
                f"Data API v2 tape failed: {exc}", source="data-api"
            ) from exc
        buy = sell = 0.0
        latest: float | None = None
        count = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            count += 1
            size = _float(_first(item, ("size", "usdcSize", "sizeUsd", "amount"))) or 0.0
            side = str(_first(item, ("side", "takerSide"), "") or "").upper()
            if side == "BUY":
                buy += size
            elif side == "SELL":
                sell += size
            ts = _epoch_seconds(
                _first(item, ("timestamp", "ts", "time", "created_at", "createdAt"))
            )
            if ts is not None and (latest is None or ts > latest):
                latest = ts
        total = buy + sell
        return {
            "count": count,
            "last_trade_ts": (
                datetime.fromtimestamp(latest, tz=timezone.utc).isoformat()
                if latest is not None
                else ""
            ),
            "buy_volume": round(buy, 2),
            "sell_volume": round(sell, 2),
            "flow_imbalance": round((buy - sell) / total, 4) if total > 0 else 0.0,
            "source": "data-api-v2/activity?type=TRADE",
        }

    # ---- main entry --------------------------------------------------------
    def run(self, req: dict[str, Any]) -> dict:
        """Execute one scan and return the response payload.

        Args:
            req: Validated ScanRequest fields as a plain dict.

        Returns:
            ``{"market", "hunch", "tape", ...}`` per RFC-001 §6.1.

        Raises:
            ScanError: every failure path maps to an HTTP status + code.
        """
        marketplace = str(req.get("marketplace") or "polymarket")
        kind = str(req.get("kind") or "event")
        slug = str(req.get("slug") or "").strip()
        scanned_at = req.get("scanned_at")
        skew_sec: float | None = None
        if scanned_at:
            skew_sec = self._clock_skew(str(scanned_at))
        page_sha, page_ignored, page_title = self._page_state_meta(req.get("page_state"))
        received_at = utcnow_iso()

        gamma = clob = data_api = jev = None
        try:
            gamma = GammaClient(self.settings)
            resolved = self._resolve_market(gamma, kind, slug)
            market = resolved["market"]
            yes_token, no_token = resolved["tokens"]
            question = str(market.get("question") or slug)

            clob = ClobClient(self.settings)
            snap = self._book_snapshot(clob, yes_token)
            if snap is None:
                raise ScanUpstreamError("live CLOB book unavailable", source="clob")

            data_api = DataApiClient(self.settings)
            tape = self._tape(data_api, resolved["condition_id"])

            jev = JevClient(self.settings)
            try:
                decision = jev.decide(
                    DecisionState(
                        news_summary=page_title or f"Extension scan: {question}",
                        market_question=question,
                        yes_price=snap["mid_price"],
                        extra_context=f"source=extension marketplace={marketplace}",
                    )
                )
            except Exception as exc:  # noqa: BLE001 - recorded, mapped to 502
                self.audit.record(
                    "stage_error", {"stage": "jev.decide", "error": str(exc)}
                )
                raise ScanUpstreamError(
                    f"Jev decision failed: {exc}", source="jev"
                ) from exc

            state = _load_state()
            positions = state.get("positions") or []
            signal = Signal(
                market_id=resolved["condition_id"],
                token_id=yes_token,
                question=question,
                p_true=float(decision.p_true),
                market_price=snap["mid_price"],
                spread_cents=snap["spread_cents"],
                top_book_depth_usd=snap["top_book_depth_usd"],
                hours_to_resolution=_hours_to_resolution(market),
                news_ts=received_at,
                price_ts=snap["price_ts"],
                negrisk_sum=negrisk_sum(market),
                uma_dispute=_uma_dispute(market),
                jev_confidence=decision.jev_confidence,
                jev_choice=decision.choice,
                fee_category=_fee_category(market, str(market.get("eventTitle") or "")),
                book_asks=snap["book_asks"],
                book_bids=snap["book_bids"],
            )
            result = RiskGate(self.settings).evaluate(
                signal,
                _exposure_usd(positions),
                float(self.settings.PAPER_BANKROLL_USD),
                open_positions_count=len(positions),
            )

            signal_id = uuid4().hex
            scan_id = self.audit.record(
                "scan",
                {
                    "marketplace": marketplace,
                    "kind": kind,
                    "slug": slug,
                    "url": req.get("url"),
                    "scanned_at": scanned_at,
                    "received_at": received_at,
                    "clock_skew_sec": (
                        round(skew_sec, 3) if skew_sec is not None else None
                    ),
                    "page_state_sha256": page_sha,
                    "page_state_ignored": page_ignored,
                    "condition_id": resolved["condition_id"],
                    "market_slug": str(market.get("slug") or slug),
                    "signal_id": signal_id,
                    "outcome": "approved" if result.approved else "vetoed",
                    "source": "extension",
                },
            )
            gate_payload = {
                "approved": result.approved,
                "size_usd": result.size_usd,
                "edge": result.edge,
                "fee_adjusted_edge": result.fee_adjusted_edge,
                "vetoes": [
                    {"reason": veto.reason, "detail": veto.detail}
                    for veto in result.vetoes
                ],
            }
            self.audit.record(
                "signal",
                {
                    **asdict(signal),
                    "signal_id": signal_id,
                    "marketplace": marketplace,
                    "source": "extension",
                    "scan_id": scan_id,
                    "model_choice": decision.choice,
                    "model_confidence": decision.jev_confidence,
                    "model_version": decision.jev_model,
                    "model_mock": decision.mock,
                    "gate": gate_payload,
                    "trade_placed": False,
                    "tape": tape,
                },
            )
            for veto in result.vetoes:
                self.audit.record(
                    "veto",
                    {
                        "market_id": signal.market_id,
                        "question": signal.question,
                        "reason": veto.reason,
                        "detail": veto.detail,
                        "ts": utcnow_iso(),
                        "marketplace": marketplace,
                        "signal_id": signal_id,
                    },
                )

            side = buy_direction(signal)
            reason = (
                f"{side} · strength {decision.signal_strength} · "
                f"conf {decision.jev_confidence:.2f}"
            )
            if tape["buy_volume"] + tape["sell_volume"] > 0:
                reason += f" · tape imbalance {tape['flow_imbalance']:+.2f}"

            return {
                "mode": "PAPER",
                "market": {
                    "title": question,
                    "slug": str(market.get("slug") or slug),
                    "condition_id": resolved["condition_id"],
                    "event_slug": resolved["event_slug"],
                    "yes_token_id": yes_token,
                    "no_token_id": no_token,
                    "end_date": str(market.get("endDate") or market.get("end_date") or ""),
                    "hours_to_resolution": round(_hours_to_resolution(market), 4),
                    "negrisk_sum": negrisk_sum(market),
                    "uma_dispute": bool(signal.uma_dispute),
                },
                "hunch": {
                    "signal_id": signal_id,
                    "marketplace": marketplace,
                    "source": "extension",
                    "side": side,
                    "p_true": round(float(decision.p_true), 4),
                    "price": round(snap["mid_price"], 6),
                    "edge": result.edge,
                    "fee_adjusted_edge": result.fee_adjusted_edge,
                    "spread_cents": round(snap["spread_cents"], 4),
                    "top_book_depth_usd": round(snap["top_book_depth_usd"], 2),
                    "approved": result.approved,
                    "size_usd": result.size_usd,
                    "vetoes": gate_payload["vetoes"],
                    "reason": reason,
                    "model_choice": decision.choice,
                    "model_confidence": decision.jev_confidence,
                    "model_version": decision.jev_model,
                    "model_mock": decision.mock,
                    "signal_strength": decision.signal_strength,
                    "signal_score": decision.signal_score,
                    "paper": True,
                    "trade_placed": False,
                    "label": PAPER_LABEL,
                },
                "tape": tape,
                "audit_event_id": scan_id,
                "ts": received_at,
                "page_state_ignored": page_ignored,
            }
        finally:
            for client in (gamma, clob, data_api, jev):
                if client is not None:
                    try:
                        client.close()
                    except Exception:  # noqa: BLE001 - best effort
                        pass
