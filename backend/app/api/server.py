"""Monitor API for hunchfall (paper mode).

All data comes from the SQLite audit log and ``data/state.json`` written by
the trading loop. Everything is labeled PAPER. Nothing here can place a
real order, move money, or touch a wallet — the watch-only wallet registry
accepts addresses only and rejects anything resembling a secret.

RFC-001 additive routes (existing routes keep their exact behavior):

    POST /extension/scan              re-validate a page hint, record a hunch
    POST /signals/{id}/validate       record HIT/MISS, update calibration
    POST /wallets                     watch-only registry (secrets -> 400)
    GET  /wallets/{address}/activity  read-only activity (Data API v2 + RPC)
    GET  /marketplaces                per-marketplace scan/hunch/validation counts
    GET  /status                      + additive ``equity_curve`` field
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.schemas import (
    PredictRequest,
    ResolveRequest,
    ScanRequest,
    ValidateRequest,
    WalletCreateRequest,
)
from app.memory.audit import AuditLog
from app.paths import (
    read_killswitch,
    resolve_db_path,
    state_path,
    write_killswitch,
)
from app.polymarket.clob import ClobClient
from app.predict import PredictError, PredictService
from app.scan import ScanError, ScanService
from app.wallets import (
    SECRET_REJECT_MESSAGE,
    InvalidAddress,
    WalletRegistry,
    collect_activity,
    normalize_address,
    screen_for_secrets,
)


def _cors_origins(settings) -> list[str]:
    """Accept CORS_ORIGINS as a comma-separated string or a list."""
    raw = getattr(settings, "CORS_ORIGINS", "")
    if isinstance(raw, (list, tuple)):
        return [str(o).strip() for o in raw if str(o).strip()]
    return [o.strip() for o in str(raw).split(",") if o.strip()]


def _read_state() -> dict:
    """Read the loop-written state snapshot; honest placeholder if absent."""
    p = state_path()
    if not p.exists():
        return {
            "mode": "PAPER",
            "note": "no state yet — the loop has not run",
            "positions": [],
            "signals": [],
            "vetoes": [],
            "fills": [],
            "calibration": {"note": "no calibration yet", "bins": []},
        }
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {
            "mode": "PAPER",
            "note": "state.json unreadable",
            "positions": [],
            "signals": [],
            "vetoes": [],
            "fills": [],
            "calibration": {"note": "no calibration yet", "bins": []},
        }


def _iso_now() -> str:
    """Current UTC time as ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


def _mask_submission(value: str) -> str:
    """Mask a rejected submission for the audit trail — never the raw value."""
    text = str(value or "")
    if len(text) >= 10 and text[:2].lower() == "0x":
        return f"{text[:6]}…{text[-4:]}"
    return "[redacted]"


def _calibration_from_validations(rows: list[dict]) -> dict:
    """Compute calibration stats from stored validation payloads.

    A validation records HIT/MISS for the predicted side. ``p_true`` is
    always P(YES), so the implied YES outcome is HIT for side YES and the
    inverse for side NO. Rows without a directional side are excluded from
    the bins (still counted in ``n_validated`` and reported as abstention).

    Args:
        rows: Validation event dicts (newest first).

    Returns:
        Calibration block for ``GET /calibration`` / validate responses.
    """
    usable: list[dict] = []
    skipped = 0
    for event in rows:
        payload = event.get("payload") or {}
        p_true = payload.get("p_true")
        side = str(payload.get("side") or "").upper()
        outcome = str(payload.get("outcome") or "").upper()
        if (
            not isinstance(p_true, (int, float))
            or side not in ("YES", "NO")
            or outcome not in ("HIT", "MISS")
        ):
            skipped += 1
            continue
        yes_outcome = 1.0 if (outcome == "HIT") == (side == "YES") else 0.0
        usable.append(
            {"p_true": float(p_true), "yes_outcome": yes_outcome, "outcome": outcome}
        )
    n = len(usable)
    hits = sum(1 for row in usable if row["outcome"] == "HIT")
    bins: list[dict] = []
    for i in range(10):
        lo, hi = i / 10.0, (i + 1) / 10.0
        in_bin = [row for row in usable if lo <= row["p_true"] < hi + 1e-12]
        if i < 9:
            in_bin = [row for row in in_bin if row["p_true"] < hi]
        count = len(in_bin)
        bins.append(
            {
                "bin_mid": round((lo + hi) / 2.0, 4),
                "mean_pred": (
                    round(sum(row["p_true"] for row in in_bin) / count, 4)
                    if count
                    else None
                ),
                "empirical": (
                    round(sum(row["yes_outcome"] for row in in_bin) / count, 4)
                    if count
                    else None
                ),
                "n": count,
            }
        )
    total = len(rows)
    return {
        "n_validated": total,
        "n_scored": n,
        "bins": bins,
        "accuracy": round(hits / n, 4) if n else None,
        "brier": (
            round(
                sum(
                    (row["p_true"] - row["yes_outcome"]) ** 2 for row in usable
                )
                / n,
                6,
            )
            if n
            else None
        ),
        "abstention_rate": round(skipped / total, 4) if total else None,
        "note": "paper validations only — HIT/MISS verdicts from the review UI",
    }


def create_app(settings) -> FastAPI:
    """Build the FastAPI app.

    Args:
        settings: App Settings (CORS_ORIGINS, DATABASE_PATH, ...).

    Returns:
        Configured FastAPI application.
    """
    app = FastAPI(
        title="hunchfall (paper)",
        version="0.1.0",
        description="Paper-trading monitor API. All data is simulated; "
        "nothing here places real orders.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(settings) or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    db_path = resolve_db_path(settings.DATABASE_PATH)
    audit = AuditLog(db_path)
    wallets = WalletRegistry(db_path)
    scan_service = ScanService(settings, audit)
    predict_service = PredictService(settings, audit)
    app.state.settings = settings
    app.state.audit = audit
    app.state.wallets = wallets
    app.state.scan = scan_service
    app.state.predict = predict_service

    @app.get("/")
    def root() -> dict:
        return {"service": "hunchfall", "mode": "PAPER", "docs": "/docs"}

    @app.get("/status")
    def status() -> dict:
        """Bankroll, exposure, equity, kill-switch state — all paper.

        RFC-001 additive field: ``equity_curve`` from ``cycle_end`` audit
        events (≤200 most recent cycles, ascending). Empty when the loop
        has never completed a cycle — never synthesized.
        """
        state = _read_state()
        return {
            "service": "hunchfall",
            "mode": "PAPER",
            "bankroll_usd": state.get("bankroll_usd"),
            "cash_usd": state.get("cash_usd"),
            "exposure_usd": state.get("exposure_usd"),
            "equity_usd": state.get("equity_usd"),
            "drawdown_pct": state.get("drawdown_pct"),
            "kill_switch": read_killswitch(),
            "updated_at": state.get("updated_at"),
            "audit": audit.get_status(),
            "equity_curve": audit.equity_curve(),
            "equity_curve_source": "cycle_end audit events",
            "equity_curve_note": "paper equity only; ≤200 most recent cycles, ascending",
        }

    @app.get("/positions")
    def positions() -> dict:
        return {"mode": "PAPER", "positions": _read_state().get("positions", [])}

    @app.get("/signals")
    def signals(limit: int = 200) -> dict:
        return {"mode": "PAPER", "signals": audit.query("signal", limit=limit)}

    @app.get("/vetoes")
    def vetoes(limit: int = 200) -> dict:
        return {"mode": "PAPER", "vetoes": audit.query("veto", limit=limit)}

    @app.get("/fills")
    def fills(limit: int = 200) -> dict:
        return {"mode": "PAPER", "fills": audit.query("fill", limit=limit)}

    @app.get("/calibration")
    def calibration() -> dict:
        """Calibration stats derived from validations (paper only).

        When no validations exist yet, the loop-written ``state.json``
        placeholder is shown instead — same shape, honest zeros.
        """
        rows = audit.validations()
        if rows:
            return {"mode": "PAPER", "calibration": _calibration_from_validations(rows)}
        return {
            "mode": "PAPER",
            "calibration": _read_state().get(
                "calibration", {"note": "no calibration yet", "bins": []}
            ),
        }

    @app.post("/kill")
    def kill(body: dict[str, Any] | None = None) -> dict:
        """Engage the kill switch (manual). The loop halts on its next check."""
        reason = (body or {}).get("reason", "manual")
        record = write_killswitch(True, str(reason))
        audit.record("killswitch", {"manual": True, "reason": record["reason"]})
        return {"mode": "PAPER", "kill_switch": record}

    @app.post("/resume")
    def resume() -> dict:
        """Clear the kill switch so the loop may run again."""
        record = write_killswitch(False, None)
        audit.record("killswitch", {"manual": True, "reason": "resumed"})
        return {"mode": "PAPER", "kill_switch": record}

    @app.get("/config")
    def config() -> dict:
        """Non-secret operational config (keys are stripped by safe_subset)."""
        return {"mode": "PAPER", "config": settings.safe_subset()}

    # --------------------------------------------------------------------- #
    # RFC-001 additions                                                     #
    # --------------------------------------------------------------------- #

    @app.post("/extension/scan")
    def extension_scan(req: ScanRequest) -> dict:
        """Re-validate a page hint and record a paper prediction.

        Prediction only — no (even simulated) fill is placed here. Prices
        come from the live CLOB and trades from Data API v2; the scraped
        ``page_state`` is never trusted.
        """
        try:
            return scan_service.run(req.model_dump())
        except ScanError as exc:
            detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
            if exc.source:
                detail["source"] = exc.source
            raise HTTPException(status_code=exc.status, detail=detail) from exc

    @app.post("/signals/{signal_id}/validate")
    def validate_signal(signal_id: str, req: ValidateRequest) -> dict:
        """Record a HIT/MISS verdict for a stored signal (append-only).

        ``{signal_id}`` is the extension scan's uuid or an audit row id for
        loop-written signals (``"<id>"`` or ``"row:<id>"``). Duplicate
        verdicts are rejected with 409; calibration updates are derived on
        read from validation events.
        """
        row = audit.signal_by_ref(signal_id)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "unknown_signal",
                    "message": f"no signal matches {signal_id!r}",
                },
            )
        payload = row.get("payload") or {}
        ref = str(payload.get("signal_id") or f"row:{row['id']}")
        previous = audit.previous_validation(ref)
        if previous is not None:
            prev_payload = previous.get("payload") or {}
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "already_validated",
                    "message": (
                        f"signal {ref} was already validated as "
                        f"{prev_payload.get('outcome')}"
                    ),
                },
            )

        p_true = payload.get("p_true")
        market_price = payload.get("market_price")
        side = "YES"
        if isinstance(p_true, (int, float)) and isinstance(market_price, (int, float)):
            side = "YES" if p_true > market_price else "NO"

        price_at_validate: float | None = None
        direction_matched: bool | None = None
        token_id = payload.get("token_id")
        if isinstance(token_id, str) and token_id:
            clob = None
            try:
                clob = ClobClient(settings)
                price_at_validate = float(clob.get_mid_price(token_id))
                if isinstance(market_price, (int, float)):
                    direction_matched = (
                        price_at_validate > float(market_price)
                        if side == "YES"
                        else price_at_validate < float(market_price)
                    )
            except Exception as exc:  # noqa: BLE001 - evidence is best-effort
                audit.record(
                    "stage_error", {"stage": "clob.recheck", "error": str(exc)}
                )
            finally:
                if clob is not None:
                    try:
                        clob.close()
                    except Exception:  # noqa: BLE001 - best effort
                        pass

        validated_at = _iso_now()
        audit.record(
            "validation",
            {
                "signal_ref": ref,
                "signal_id": payload.get("signal_id"),
                "marketplace": str(payload.get("marketplace") or "polymarket"),
                "outcome": req.outcome,
                "validated_at": validated_at,
                "p_true": p_true,
                "side": side,
                "market_price_at_signal": market_price,
                "price_at_validate": price_at_validate,
                "direction_matched": direction_matched,
                "model_version": payload.get("model_version"),
                "model_mock": payload.get("model_mock"),
                "source": payload.get("source") or "loop",
            },
        )
        calibration = _calibration_from_validations(audit.validations())
        return {
            "mode": "PAPER",
            "signal_id": ref,
            "outcome": req.outcome,
            "validated_at": validated_at,
            "signal": {
                "question": payload.get("question", ""),
                "side": side,
                "p_true": p_true,
                "market_price": market_price,
                "model_mock": payload.get("model_mock"),
                "model_version": payload.get("model_version"),
                "source": payload.get("source") or "loop",
            },
            "evidence": {
                "price_at_validate": price_at_validate,
                "direction_matched": direction_matched,
                "note": (
                    "best-effort CLOB re-check; the recorded verdict is "
                    "authoritative"
                ),
            },
            "calibration": {
                "n_validated": calibration["n_validated"],
                "accuracy": calibration["accuracy"],
                "brier": calibration["brier"],
            },
            "label": "paper prediction · no trade placed",
        }

    @app.post("/wallets")
    def create_wallet(req: WalletCreateRequest):
        """Register a watch-only address (never accepts secrets)."""
        for field_name, value in (("address", req.address), ("label", req.label)):
            if screen_for_secrets(value):
                audit.record(
                    "wallet_secret_rejected",
                    {
                        "field": field_name,
                        "reason": "secret_like_input",
                        "address_masked": _mask_submission(req.address),
                    },
                )
                raise HTTPException(
                    status_code=400,
                    detail={"code": "secret_rejected", "message": SECRET_REJECT_MESSAGE},
                )
        try:
            address = normalize_address(req.address)
        except InvalidAddress as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "invalid_address",
                    "message": "expected a 0x-prefixed 40-hex EVM address",
                },
            ) from exc
        label = req.label.strip()
        if not label or len(label) > int(settings.WALLET_LABEL_MAX_CHARS):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "invalid_label",
                    "message": (
                        f"label must be 1..{settings.WALLET_LABEL_MAX_CHARS} characters"
                    ),
                },
            )
        wallet, created = wallets.upsert(address, label)
        audit.record(
            "wallet_registered",
            {"address": address, "label": label, "created": created},
        )
        return JSONResponse(
            status_code=201 if created else 200,
            content={
                "mode": "PAPER",
                "created": created,
                "wallet": wallet,
                "note": (
                    "watch-only registry — never accepts or stores keys, "
                    "mnemonics, or signing material"
                ),
            },
        )

    @app.get("/wallets/{address}/activity")
    def wallet_activity(address: str) -> dict:
        """Read-only activity for a registered watch-only address.

        Sources: Polymarket Data API v2 (activity + open positions) and a
        public Polygon RPC (balance + nonce). Partial failures degrade
        gracefully (``degraded``); only a total outage returns 502.
        """
        if screen_for_secrets(address):
            raise HTTPException(
                status_code=400,
                detail={"code": "secret_rejected", "message": SECRET_REJECT_MESSAGE},
            )
        try:
            addr = normalize_address(address)
        except InvalidAddress as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "invalid_address",
                    "message": "expected a 0x-prefixed 40-hex EVM address",
                },
            ) from exc
        wallet = wallets.get(addr)
        if wallet is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "wallet_not_registered",
                    "message": f"{addr} is not in the watch-only registry",
                },
            )
        result = collect_activity(settings, wallet)
        ok = bool(result.pop("ok", False))
        audit.record(
            "wallet_activity_request",
            {
                "address": addr,
                "sources": result.get("sources", []),
                "degraded": result.get("degraded", []),
                "activity": len(result.get("activity") or []),
                "positions": len(result.get("positions") or []),
            },
        )
        if not ok:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "upstream_error",
                    "message": "wallet activity sources unavailable",
                    "source": "data-api|polygon-rpc",
                },
            )
        return result

    @app.get("/marketplaces")
    def marketplaces() -> dict:
        """Scans / hunches / validations grouped per marketplace."""
        return {
            "mode": "PAPER",
            "label": "paper predictions only",
            "marketplaces": audit.marketplace_counts(),
        }

    # --------------------------------------------------------------------- #
    # RFC-003 additions — market guesser (prediction only)                  #
    # --------------------------------------------------------------------- #

    def _predict_http_error(exc: PredictError) -> HTTPException:
        """Map a PredictError to the RFC-001 error envelope."""
        detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
        if exc.source:
            detail["source"] = exc.source
        return HTTPException(status_code=exc.status, detail=detail)

    @app.post("/predict")
    def predict(req: PredictRequest) -> dict:
        """Guess a market's outcome: P(YES), direction, confidence — or abstain.

        Prediction only. Gamma + CLOB are required (502 + nothing persisted on
        failure); the tape and the social pulse are features. Every response
        carries ``label`` + ``trade_placed:false``.
        """
        try:
            return predict_service.run(req.model_dump())
        except PredictError as exc:
            raise _predict_http_error(exc) from exc

    @app.get("/predict/demo")
    def predict_demo() -> dict:
        """Run the pinned demo market; canned fallback. Always 200, never persists."""
        return predict_service.demo()

    @app.get("/predict/accuracy")
    def predict_accuracy(include_mock: bool = False, limit: int = 500) -> dict:
        """Derived-on-read calibration ledger (mock excluded by default).

        ``limit`` is clamped: it is a scan cap, not a way to ask the audit
        log for an unbounded page.
        """
        return predict_service.accuracy(
            include_mock=include_mock, limit=max(1, min(int(limit), 5000))
        )

    @app.post("/predict/{prediction_id}/resolve")
    def predict_resolve(prediction_id: str, req: ResolveRequest) -> dict:
        """Record the realised YES/NO outcome for one logged prediction."""
        try:
            return predict_service.resolve_prediction(prediction_id, req.outcome)
        except PredictError as exc:
            raise _predict_http_error(exc) from exc

    return app
