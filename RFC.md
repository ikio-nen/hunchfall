# RFC.md — hunchfall RFCs

> This file collects accepted RFCs. **RFC-001** (extension scan, signal
> validation, watch-only wallets, marketplaces, proof dashboard) is below and
> unchanged. **RFC-003** (market guesser MVP — prediction only) follows it at
> the end of this file.

---

# RFC-001 — Extension Scan, Signal Validation, Watch-Only Wallets, Marketplaces & Proof Dashboard

| | |
|---|---|
| **RFC** | RFC-001 |
| **Status** | **DONE** — approved 2026-09-30; implemented in Phase 2; merged via PR [#1](https://github.com/ikio-nen/hunchfall/pull/1) (rebase-merge `bd5a0f8`), CI green |
| **Repo / branch** | `D:\hunchfall` @ `main` |
| **Date** | 2026-09-30 |
| **Deliverable of Phase 1** | This file at `D:\hunchfall\RFC.md`. **No implementation code in this phase.** |

> **Phase 2 complete (2026-09-30):** all planned routes, wallet surfaces, and dashboard tabs were implemented, rebase-merged via PR #1, and confirmed green in CI — this section is kept as the historical record of what was planned and built.
>
> **APPROVAL SCOPE:** On approval, the only action is writing this document to `RFC.md` at the repo root, then stopping. Phase 2 (implementation) starts only after a separate explicit approval.

---

## 1. Summary

Five additive FastAPI routes plus one additive `/status` field, and five dashboard surfaces (three new tabs, two upgraded). Everything remains paper-only: no order endpoints, no signing, no keys, no real money.

**User decisions incorporated (locked):**
1. **Equity curve lives inside `GET /status`** as an additive `equity_curve` field — no new route; keeps the route list at exactly the five requested endpoints.
2. **`POST /extension/scan` is prediction-only** — it records the signal + gate outcome and never runs `PaperEngine`. Every extension hunch is a prediction; positions continue to come only from the loop.
3. **Wallet activity = Polymarket Data API v2 + public Polygon RPC balance**, with graceful degradation (partial failure → 200 + `degraded` list; total failure → 502).

Also incorporated: the user-supplied **verified Data API v2 params block** (§3), which resolves the previously unverified parameter surface.

---

## 2. Invariants (from `hunchfall-safety`, now fixed at `.agents/skills/hunchfall-safety/SKILL.md`)

1. **Paper only.** No real order, no signing, no wallet custody. `POST /wallets` accepts watch-only addresses ONLY; anything resembling a secret → 400, never stored, never echoed, never logged.
2. **Official APIs only** for market data: Gamma, CLOB (market-data endpoints only), Data API v2.
3. **Jev emits typed values only**; the deterministic gate owns the decision and can never be overridden by the model.
4. **The extension stays read-only** — it never clicks, fills, signs, or submits on marketplace pages. Every prediction surface displays **“paper prediction · no trade placed.”**
5. Fixed risk policy unchanged (10% fee-adjusted edge, quarter-Kelly, 10%/40%/max-5 caps, −15% auto-kill, UMA veto, fail closed).
6. Every new endpoint ships with 1 happy-path + 2 negative tests; `python -m app.loop --once` and the full pytest suite stay green; new env vars go in `.env.example` only.

*Housekeeping note: `.agents/skills/hunchfall-safety/SKILL safe.md` is a stale duplicate of the api-contracts skill; the real safety skill is `SKILL.md`. Deleting the stale file is optional cleanup (Phase 2, docs-only).*

---

## 3. Verified external data contract (Data API v2)

Source: official `openapi.json` for `data-api.polymarket.com`, pasted by the reviewer. This is now the authoritative parameter set; nothing below is guessed.

**Global v2 rules**
- Responses are wrapped in `{"data": ..., "pagination": {"next_cursor": ...}}`.
- **No `offset` param** — sending one returns 400. Pagination is cursor-based.
- Params accept snake_case **and** camelCase.
- 429 responses carry `Retry-After`; clients must wait and retry.
- A documented miss returns an **empty `data` array, not an error** — treat as a valid zero-state.
- No auth; all public. **v1 retires Oct 24, 2026** — all new code uses `/v2/*` only.

**`GET /v2/activity` — wallet activity feed**

| Param | Rules |
|---|---|
| `user` | **REQUIRED**, EVM address; the feed is user-anchored (400 if missing) |
| `type` | comma-separated: `TRADE`, `SPLIT`, `MERGE`, `REDEEM`, `DEPOSIT`, `WITHDRAWAL`, …; `TIP` is opt-in only (HUNCHFALL never requests it) |
| `condition` | up to 20 comma-separated condition ids; aliases `condition_id` / `conditionId` accepted |
| `event_id` | up to 20; **mutually exclusive with `condition`** |
| `side` | `BUY` / `SELL` |
| `start` / `end` | epoch seconds; omitted `start` defaults to 3 years back |
| `limit` | default 100, max 1000 |
| `cursor` | opaque `next_cursor`; re-send identical filters per page |
| `sort_direction` | `ASC` / `DESC` (default DESC) |
| `exclude_deposits_withdrawals` | default `true` |

**`GET /v2/positions`** — `?user=ADDRESS&status=OPEN|CLOSED&limit=N` (one route serves the whole lifecycle).

**Design consequences (load-bearing):**
- **Trade tape for `/extension/scan` uses `GET /v2/activity?type=TRADE&condition=<condition_id>&limit=N`**, not `/v2/trades` — the activity filters are verified, the `/v2/trades` query surface is not. This directly satisfies “re-validate trades via Data API v2”.
- **Wallet activity** uses `/v2/activity?user=<address>&limit=N&sort_direction=DESC` and `/v2/positions?user=<address>&status=OPEN&limit=N`.
- If the registered address is not a Polymarket proxy wallet, the API returns empty arrays — handled as **“no activity”**, not an error.
- New client code honors `Retry-After` (≤2 retries, sleep capped at 5 s), never sends `offset`, and unwraps `{"data", "pagination"}`.
- Response item field names are normalized defensively (snake_case primary, camelCase fallback); the normalizer is pinned by tests.

---

## 4. Architect decisions

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Equity curve as additive `equity_curve` on `GET /status`, derived from `cycle_end` audit events | Reviewer decision; no new route; data already exists in the append-only log | New `GET /equity` route |
| D2 | Scan is prediction-only: persist signal + gate result; **never execute** | Reviewer decision; keeps “no trade placed” literal; no fill-path surface to test | Auto paper-fill on approval |
| D3 | Activity = Data API v2 (activity + positions) ⊕ Polygon RPC balance/nonce, degraded gracefully | Reviewer decision; product-relevant data + independent on-chain evidence, all keyless | Single-source variants |
| D4 | Reuse `loop.py` helpers by importing `_market_tokens`, `_snapshot`, `_hours_to_resolution`, `_fee_category`, `_uma_dispute`, `_exposure_usd` from `app.loop` — **zero edits to `loop.py`** | Literal “additive only”; no behavioral risk to the trading loop. Accepted coupling; promote to `app/analysis.py` in a later refactor | Extracting helpers now (large diff, zero loop tests) |
| D5 | `page_state` is **hashed + redacted, never stored raw**; only a sanitized `title` (≤300 chars) may inform the Jev context | It is untrusted page content; raw storage risks secret material and body bloat | Storing raw page_state |
| D6 | `news_ts` = server receipt time; client `scanned_at` is drift-checked (≤ `SCAN_CLOCK_SKEW_SEC`, default 300 s) and recorded, but never trusted as truth | Server clock is authoritative; keeps the gate’s news/price-skew rule meaningful and fail-closed | Trusting client timestamps |
| D7 | Canonical signal ref = payload `signal_id` (extension, uuid4) **or** `row:<audit id>` (loop signals) | Makes HIT/MISS validation work for every persisted signal, including older loop signals | UUID-only (breaks loop signals) |
| D8 | Validation appends a `validation` event; duplicate → 409; calibration is **derived on read** from signals + validations | Preserves the append-only audit guarantee (no mutation of signal rows); `state.json` untouched | Mutating signal payloads / new calibration table |
| D9 | Trade tape via verified `/v2/activity?type=TRADE` (§3) | Avoids the one unverified surface before the Oct 24 v1 sunset | `/v2/trades` with guessed params |

---

## 5. `POST /extension/scan` pipeline

```
request ──► schema/clock validation ──► Gamma resolve (slug→market|event→market)
        ──► CLOB book snapshot (mid, spread, depth, book levels)          ← re-validated, authoritative
        ──► Data API v2 /v2/activity?type=TRADE&condition=… (tape)         ← re-validated, informational
        ──► Jev typed decision (mock when JEV_MOCK=true)                   ← model never sees page prices
        ──► RiskGate.evaluate(...)                                         ← deterministic, all vetoes collected
        ──► audit: scan + signal (+ veto per reason)                       ← append-only
        ──► response: market + hunch + tape                                ← approved or vetoed, always 200 if recorded
```

Rules:
- `page_state.yesPrice/noPrice` are **ignored** and listed in `page_state_ignored`; the response `price` always equals the CLOB mid.
- Gate inputs: `market_price` = CLOB mid, `spread_cents`/`top_book_depth_usd`/book levels from the live book, `hours_to_resolution` via the loop helper (unknown → 0.0 → fail-closed `near_resolution`), `negrisk_sum` from Gamma `outcomePrices`, `uma_dispute` from the loop helper, `jev_confidence`/`jev_choice` from the typed decision, `fee_category` via the loop helper, `news_ts` = receipt time, `price_ts` = snapshot time. Exposure/bankroll/open-position caps read the loop’s `state.json` — an extension hunch can be vetoed by portfolio caps; that is intentional.
- `reason` is composed from typed fields only, e.g. `"YES · strength medium · conf 0.78 · tape imbalance +0.31"` — never model text (the model emits none).
- A veto is **not** an error: the prediction is persisted and returned with `approved: false` + the full veto list.
- Closed/resolved markets and markets without two tradeable token ids are not scannable → 409 (no signal persisted).
- Upstream failure (Gamma/CLOB/Data API/Jev) → 502 with `source` named; a `stage_error` audit event; **no signal persisted** (fail closed).

---

## 6. Endpoint contracts

Error envelope everywhere: `{"detail": {"code": "...", "message": "...", "source"?: "gamma|clob|data-api|jev|polygon-rpc"}}`.

### 6.1 `POST /extension/scan`

Request (pydantic `ScanRequest`, `extra="forbid"`):

| Field | Type / rules |
|---|---|
| `marketplace` | str, default `"polymarket"`, pattern `^[a-z0-9][a-z0-9_-]{0,31}$` |
| `kind` | `"event"` \| `"market"`, default `"event"` |
| `slug` | str, 1–160, pattern `^[A-Za-z0-9._~-]+$` (required) |
| `url` | str \| null, ≤1024, must start `http://` or `https://` |
| `page_state` | object \| null, canonical JSON ≤4096 bytes |
| `scanned_at` | ISO-8601 str \| null; if present, \|server−client\| > 300 s → 400 `clock_skew` |

```json
// 200 (vetoed example; approved identical with approved=true, size_usd>0, vetoes:[])
{
  "mode": "PAPER",
  "market": {
    "title": "Will BTC hit $100k in 2026?", "slug": "will-btc-hit-100k-in-2026",
    "condition_id": "0x…", "event_slug": "…",
    "yes_token_id": "…", "no_token_id": "…",
    "end_date": "2026-12-31T23:59:59Z", "hours_to_resolution": 2201.4,
    "negrisk_sum": 1.0, "uma_dispute": false
  },
  "hunch": {
    "signal_id": "9b2f…", "marketplace": "polymarket", "source": "extension",
    "side": "YES", "p_true": 0.68, "price": 0.62, "edge": 0.06,
    "fee_adjusted_edge": 0.041, "spread_cents": 2.0, "top_book_depth_usd": 5210.5,
    "approved": false, "size_usd": 0.0,
    "vetoes": [{"reason": "edge_too_small", "detail": "fee-adjusted edge 0.0410 < EDGE_THRESHOLD 0.10"}],
    "reason": "YES · strength medium · conf 0.78 · tape imbalance +0.31",
    "model_choice": "YES", "model_confidence": 0.78, "model_version": "jev-mock",
    "model_mock": true, "signal_strength": "medium", "signal_score": 0.67,
    "paper": true, "trade_placed": false,
    "label": "paper prediction · no trade placed"
  },
  "tape": {"count": 5, "last_trade_ts": "2026-09-30T07:01:11Z",
           "buy_volume": 1200.0, "sell_volume": 640.0, "flow_imbalance": 0.30,
           "source": "data-api-v2/activity?type=TRADE"},
  "audit_event_id": 142, "ts": "2026-09-30T07:05:00Z",
  "page_state_ignored": ["yesPrice", "noPrice"]
}
```

| Status | Code | When |
|---|---|---|
| 200 | — | Prediction recorded (approved or vetoed) |
| 400 | `bad_request` / `clock_skew` | Schema-level rules; scanner clock drift > 300 s |
| 404 | `market_not_found` | Gamma has no market/event for the slug |
| 409 | `market_not_tradeable` | Closed/resolved, or missing a two-sided token pair |
| 422 | pydantic | Unknown fields, bad `kind`, malformed slug |
| 502 | `upstream_error` | Gamma/CLOB/Data API/Jev failure (no signal persisted) |

**Persisted events:** `scan` (marketplace, kind, slug, url, `scanned_at`, `received_at`, `clock_skew_sec`, `page_state_sha256`, `page_state_ignored`, resolved market ids, outcome), `signal` (full gate `Signal` + `signal_id`, `marketplace`, `source:"extension"`, `scan_id`, model fields, `gate{approved,size_usd,edge,fee_adjusted_edge,vetoes}`, `trade_placed:false`, tape summary), `veto` (one per reason), `stage_error` on upstream failure.

### 6.2 `POST /signals/{id}/validate`

- `{id}` = canonical ref: uuid **or** integer audit id (loop signals).
- Request: `{"outcome": "HIT" | "MISS"}` (`extra="forbid"`).
- Side effect: append `validation` event; duplicate ref → 409. A best-effort CLOB re-check records `price_at_validate`/`direction_matched` as evidence only — **the recorded HIT/MISS verdict is authoritative**, and a failed re-check never blocks the verdict.

```json
// 200
{
  "mode": "PAPER", "signal_id": "9b2f…", "outcome": "HIT",
  "validated_at": "2026-09-30T08:00:00Z",
  "signal": {"question": "…", "side": "YES", "p_true": 0.68, "market_price": 0.62,
             "model_mock": true, "model_version": "jev-mock", "source": "extension"},
  "evidence": {"price_at_validate": 0.71, "direction_matched": true,
               "note": "best-effort CLOB re-check; the recorded verdict is authoritative"},
  "calibration": {"n_validated": 1, "accuracy": 1.0, "brier": 0.102},
  "label": "paper prediction · no trade placed"
}
```

| Status | Code | When |
|---|---|---|
| 404 | `unknown_signal` | No signal event matches the ref |
| 409 | `already_validated` | A `validation` event exists for the ref (previous outcome returned) |
| 422 | pydantic | `outcome` not HIT/MISS |

**Calibration (derived on read):** 10 equal-width bins over `p_true`; `yes_outcome = (HIT?1:0)` when `side=="YES"`, inverted when `side=="NO"`; unknown side excluded from bins (still counted in `n_validated`); `accuracy = HIT/(HIT+MISS)`, `brier = mean((p_true − yes_outcome)²)`. `GET /calibration` returns the computed block when validations exist, otherwise the existing `state.json` placeholder — same shape, additive fields. Hand-computed tests pin the math.

### 6.3 `POST /wallets`

Request: `{"address": "0x…", "label": "Ikio main"}` (`extra="forbid"`; label 1–64 printable chars).

Validation order: length/type → **secret screen over `address` and `label`** → address regex → store lowercase (EIP-55 checksum not verified — no keccak dependency; documented).

| Status | Body | When |
|---|---|---|
| 201 | `{"mode","created":true,"wallet":{address,label,watch_only:true,created_at,updated_at},"note":"watch-only registry — never accepts or stores keys, mnemonics, or signing material"}` | New registration |
| 200 | same with `created:false` | Re-registration (label upsert) |
| 400 | `{"detail":{"code":"secret_rejected","message":"watch-only addresses only — private keys, mnemonics, and signing material are never accepted or stored"}}` | Input resembles a 64-hex key, 12/15/18/21/24-word mnemonic, WIF, `xprv/xpub`, `-----BEGIN`, or key-material keywords. **Input is never echoed back.** |
| 400 | `{"detail":{"code":"invalid_address","message":"expected a 0x-prefixed 40-hex EVM address"}}` | Not secret-like but not a valid address |

Secret screen (conservative, multi-hit): whole-string or whitespace-delimited 64-hex token; ≥12 whitespace-separated all-alpha words; `xprv|xpub|mnemonic|seed phrase|private key|privatekey|keystore|-----BEGIN`; control characters. Rejection logs a `wallet_secret_rejected` audit event with `{field, reason, address_masked}` only — never the input.

### 6.4 `GET /wallets/{address}/activity`

- Path address validated first: secret-like → 400 `secret_rejected`; unregistered → 404 `wallet_not_registered` (**no outbound calls made**).
- Sources: `/v2/activity?user=…&limit=N&sort_direction=DESC`, `/v2/positions?user=…&status=OPEN&limit=N`, Polygon RPC `eth_getBalance` + `eth_getTransactionCount`. In-process TTL cache (default 60 s); empty Data API arrays are a valid “no activity” zero-state.

```json
// 200
{
  "mode": "PAPER",
  "wallet": {"address": "0x…", "label": "Ikio main", "watch_only": true},
  "sources": ["polymarket-data-api-v2", "polygon-rpc"],
  "degraded": [],
  "activity": [{"source": "polymarket-data-api-v2", "type": "TRADE", "ts": "…",
                "market": "…", "side": "BUY", "outcome": "YES",
                "size_usd": 120.0, "price": 0.62, "tx_hash": "0x…"}],
  "positions": [],
  "chain": {"source": "polygon-rpc", "rpc_url": "https://polygon-rpc.com",
            "balance_wei": "123…", "balance_pol": 1.23, "nonce": 42},
  "fetched_at": "…",
  "note": "no activity found (address may not be a Polymarket proxy wallet)",
  "label": "watch-only · read-only"
}
```

| Status | Code | When |
|---|---|---|
| 404 | `wallet_not_registered` | Address not in the registry |
| 400 | `secret_rejected` | Path input looks like a secret |
| 200 + `degraded` | — | One source fails; failed section null/empty, `degraded` names it |
| 502 | `upstream_error` | **All** sources fail (no fabricated data) |

### 6.5 `GET /marketplaces`

```json
// 200 (empty DB → {"mode":"PAPER","marketplaces":[],"label":"paper predictions only"})
{"mode": "PAPER", "label": "paper predictions only", "marketplaces": [
  {"name": "polymarket", "scans": 12, "hunches": 8, "validated": 5,
   "pending_validations": 3, "last_scan_at": "2026-09-30T07:05:00Z"}
]}
```

- `scans` = `scan` events; `hunches` = `signal` events; `validated` = `validation` events. Grouping uses payload `marketplace`, defaulting **`"polymarket"`** for legacy loop signals that lack the field; arbitrary marketplace strings are preserved, never dropped. `pending_validations = max(0, hunches − validated)`. Counting reads all persisted events (Python-side aggregation; no SQL JSON extension dependency).

### 6.6 `GET /status` (additive field only)

All existing fields unchanged; adds:

```json
{"equity_curve": [{"ts": "2026-09-29T12:00:00Z", "equity_usd": 10042.2}, "…"],
 "equity_curve_source": "cycle_end audit events", "equity_curve_note": "paper equity only; ≤200 most recent cycles, ascending"}
```

No `cycle_end` events → `[]` (honest empty, never synthesized). Other endpoints (`/signals`, `/vetoes`, `/fills`, `/calibration`, `/kill`, `/resume`, `/config`, `/positions`) keep their exact current behavior and shapes.

---

## 7. File-by-file change list

### Backend — new files

| File | Contents |
|---|---|
| `backend/app/api/schemas.py` | Pydantic request models: `ScanRequest`, `ValidateRequest`, `WalletCreateRequest` (`extra="forbid"`); helpers for ISO parsing / size caps |
| `backend/app/scan.py` | `ScanService(settings, audit)`: slug→market resolve, book snapshot, v2 tape, Jev call, gate evaluation, event persistence; imports loop helpers per D4; **never imports `PaperEngine`** |
| `backend/app/wallets.py` | `screen_for_secrets()`, `normalize_address()`, `WalletRegistry` (SQLite), `collect_activity(settings, wallet, …)` assembler with degrade logic |
| `backend/app/polygon/__init__.py`, `backend/app/polygon/rpc.py` | `PolygonRpcClient`: JSON-RPC over httpx, `eth_getBalance` + `eth_getTransactionCount` only; timeout 10 s; raises `PolygonRpcError` naming the endpoint; **no write methods exist** |
| `backend/tests/test_wallets_screening.py`, `test_api_scan.py`, `test_api_validate.py`, `test_api_wallets.py`, `test_api_marketplaces.py`, `test_api_status_equity.py`, `test_data_api_v2.py` | Test suite per §9 |

### Backend — changed files (all additive)

| File | Change |
|---|---|
| `backend/app/api/server.py` | Mount the 5 routes; `app.state.wallets`, `app.state.scan`; `/status` gains `equity_curve`; error-mapping helpers. Existing route handlers untouched |
| `backend/app/memory/audit.py` | Add read-only helpers: `query_types(types, limit)`, `event_by_id(id)`, `signal_by_ref(ref)`, `previous_validation(ref)`, `marketplace_counts()`, `equity_curve(limit=200)`, `validations(limit)`. `record/query/get_status` unchanged |
| `backend/app/polymarket/data_api.py` | Add `_get_v2()` (envelope unwrap, no `offset`, Retry-After ≤2 retries/5 s cap) + `get_activity_v2(user/condition/type/limit)`, `get_positions_v2(user, status, limit)`; existing methods untouched so the loop is unaffected |
| `backend/app/config.py` | Additive settings: `POLYGON_RPC_URL="https://polygon-rpc.com"`, `WALLET_ACTIVITY_LIMIT=50`, `WALLET_ACTIVITY_TTL_SEC=60`, `SCAN_CLOCK_SKEW_SEC=300`, `SCAN_PAGE_STATE_MAX_BYTES=4096`, `WALLET_LABEL_MAX_CHARS=64` |
| `backend/.env.example`, `README.md` env table | New variables documented (no secrets) |
| `docs/API_INVENTORY.md` | New routes; verified v2 params table; v1 sunset note |
| `docs/USER_WORKFLOW.md` | Mark `POST /signals/{id}/validate` as built (Phase 2) |

### Frontend — new files

| File | Contents |
|---|---|
| `frontend/src/pages/Marketplaces.tsx` | Cards ← `GET /marketplaces` (name, scans, hunches, validated, pending); honest empty state |
| `frontend/src/pages/Wallets.tsx` | Add form (address + label) → `POST /wallets`; secret-rejection message rendered verbatim; activity panel ← `GET /wallets/{address}/activity` with `degraded` surfaced |
| `frontend/src/pages/Prove.tsx` | Reliability diagram (existing component) + veto and fill history tables ← existing `/calibration`, `/vetoes`, `/fills` |
| `frontend/src/components/PaperPredictionBadge.tsx` | The literal badge **“paper prediction · no trade placed”** + optional MOCK/REAL/SAMPLE model label |

### Frontend — changed files (additive)

| File | Change |
|---|---|
| `frontend/src/App.tsx`, `components/Nav.tsx` | Append tabs `marketplaces`, `wallets`, `prove`; existing six tabs untouched |
| `frontend/src/api/client.ts` | `apiPostJson<T>`; interfaces (`MarketplaceCounts`, `WatchWallet`, `WalletActivity`, `EquityPoint`, validation types); backend→UI adapters (`/status` `bankroll_usd`→`bankroll`, nested `calibration`, audit-row→`Signal` with `signal_id/approved/model_mock/marketplace`) — fixes the existing live-mode shape gaps |
| `frontend/src/pages/Overview.tsx` | Consume adapter + `equity_curve` from `/status`; positions unchanged |
| `frontend/src/pages/Signals.tsx` | Columns: marketplace, model label, badge; HIT/MISS buttons → `POST /signals/{id}/validate`, disabled in SAMPLE mode and refresh on success; shows 409 as “already validated” |
| `frontend/src/pages/Calibration.tsx` | Use the adapter so live calibration renders |
| `frontend/src/index.css` | Additive `.paper-pred-badge`, cards grid, form styles |
| `frontend/src/api/sampleData.ts` | **No new synthetic data.** New surfaces receive empty fallbacks and an explicit “no sample data for this surface” note in SAMPLE mode |

---

## 8. DB / state changes

1. **New table** (same SQLite file as the audit log, created `IF NOT EXISTS` on app start):
   ```sql
   CREATE TABLE IF NOT EXISTS watch_wallets (
     address    TEXT PRIMARY KEY COLLATE NOCASE,
     label      TEXT NOT NULL,
     created_at TEXT NOT NULL,
     updated_at TEXT NOT NULL
   );
   ```
   Mutable by nature (a registry); it contains **no secret columns and never will**. This is the only non-append-only table, documented as such.
2. **`events` table: no schema change.** New `event_type` values: `scan`, `validation`, `wallet_registered`, `wallet_secret_rejected`, `wallet_activity_request`. Existing `signal`/`veto` events gain additive payload fields (`signal_id`, `marketplace`, `source`, `gate`, tape summary). Old events stay readable; readers default missing fields.
3. **`state.json`: untouched.** The loop remains its only writer; `/calibration` derives validated stats from the audit log and falls back to the existing placeholder when there are none.
4. **No migrations, no destructive operations, forward+backward compatible**: an old DB opens fine (new table ignored by old code), and a new DB works with old clients.
5. SQLite concurrency: short single-statement writes; existing default 5 s busy timeout; WAL is noted as optional hardening (P1).

---

## 9. Test plan

**Harness:** `pytest` + FastAPI `TestClient`; fakes injected by monkeypatching module-level classes (`app.scan.GammaClient/ClobClient/DataApiClient/JevClient`, `app.wallets.DataApiClient/PolygonRpcClient`); temp DB via `Settings(DATABASE_PATH=<absolute tmp path>)`; `JEV_MOCK=true` where useful; **zero network** (assert fakes were called / not called).

| Endpoint | Happy path | Negative 1 | Negative 2 |
|---|---|---|---|
| `POST /extension/scan` | Fake Gamma market + CLOB book (mid 0.50, 2¢, $5k depth) + 5 TRADE activity items + fake Jev YES/0.8 → 200; `hunch.price == 0.50` **even though `page_state.yesPrice=0.99`**; `page_state_ignored` lists both prices; signal + scan events persisted; `trade_placed:false` | Unknown slug → 404 `market_not_found`, no `signal`/`veto` events | CLOB book empty → 502 `upstream_error` `source:"clob"`, `stage_error` recorded, no signal persisted |
| `POST /signals/{id}/validate` | Seed extension signal → validate HIT → 200; `validation` event; `/calibration` shows `n_validated=1`, `accuracy=1.0`, hand-computed brier | Unknown ref → 404 `unknown_signal` | Duplicate validation → 409 `already_validated` (and outcome unchanged) |
| `POST /wallets` | Valid address + label → 201; row exists lowercased; repeat → 200 updated | 64-hex private key in `address` → 400 `secret_rejected`; **assert response body does not contain the submitted string**; no row; `wallet_secret_rejected` recorded | 12-word mnemonic in `label` → 400 `secret_rejected` (same no-echo assertion) |
| `GET /wallets/{address}/activity` | Registered wallet; fake v2 activity + positions + fake RPC balance → 200; normalized items; `chain.balance_pol`; empty v2 arrays path also asserted as “no activity” | Unregistered address → 404 `wallet_not_registered`; assert fake clients **not called** | Both sources raise → 502; partial failure → 200 + `degraded` populated |
| `GET /marketplaces` | Seed 2 scans + 2 signals + 1 validation → exact counts and `pending_validations=1` | Empty DB → 200 `{"marketplaces": []}` (nothing invented) | Legacy loop signal without `marketplace` → counted under `polymarket`; unknown marketplace name preserved |
| `GET /status` (additive) | Two `cycle_end` events → ascending `equity_curve` | No events → `[]` | — (covered by the first two) |

Extra unit tests: wallet secret-screen table (key/mnemonic/WIF/`xprv`/clean-label cases), data-api v2 unwrap + no-`offset` + Retry-After retry + empty-`data` zero-state, calibration math (hand-computed), page_state redaction (raw values never persisted).

**Frontend:** `npm run build` (`tsc -b && vite build`) in CI; manual checklist: badge on every prediction row/card, HIT/MISS round-trip incl. 409 copy, wallet secret rejection copy, `degraded` surfaced, SAMPLE mode shows no fabricated numbers on new surfaces, existing six tabs unchanged.

**Commands (Phase 2):** `cd backend && python -m pytest -q` · `cd frontend && npm run build` · offline sanity `cd backend && python -m app.loop --once` (must stay green).

---

## 10. Risks & mitigations

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| R1 | 429 rate-limit lockout (v2 or Polygon RPC) | M | M | Honor `Retry-After` (≤2 retries, ≤5 s), TTL cache on activity, CLOB already has backoff. **Blocking: retry-after test must pass** |
| R2 | Non-proxy wallet emptiness misread as error | M | L | Verified rule: empty `data` = zero-state; tests pin it |
| R3 | Secret leak via response/log/audit | L | **H** | Screen all string fields; never echo; audit stores `address_masked` only; tests assert input absent from response; no request bodies in logs |
| R4 | Poisoned/huge `page_state` | M | M | 4 KB cap; hash + redact (no raw storage); only sanitized title reaches Jev; prices never used |
| R5 | Gate vetoes most extension hunches (strict thresholds) | H | L | Expected and honest — vetoes are first-class UI; MOCK often emits SKIP → `model_skip`; tests cover both approved (controlled fake Jev) and vetoed paths |
| R6 | Private loop-helper import coupling | M | L | Zero loop edits; helper behavior pinned by tests; refactor to `app/analysis.py` noted as follow-up |
| R7 | Legacy loop signals lack `signal_id` | M | L | Canonical `row:<id>` ref; test covers validation of a loop-style signal |
| R8 | Dashboard live shapes were partially mismatched pre-existing | H | M | Adapters in `client.ts` + build + manual checklist; existing pages otherwise untouched |
| R9 | SQLite write contention (loop `--watch` + API) | L | M | Short writes, 5 s busy timeout; WAL optional P1 |
| R10 | Unauthenticated write routes (`/wallets`, validate, scan) | M | M | Same posture as existing `/kill`; local demo threat model documented; Auth0 wiring is a separate milestone (not in scope) |
| R11 | “Mock numbers” drift in UI | M | M | No synthetic data for new surfaces; MOCK/REAL/SAMPLE labels never stripped |
| R12 | v2 response item field names (our normalizer) diverge | M | M | Defensive alias parsing + normalization tests; live-check at implementation start (P0) |

---

## 11. DERISK — merge/deploy readiness (plan-stage)

No code diff exists yet, so this is an assessment of the plan and repo state, not of an implemented change.

**P0 — merge blockers before this plan is implemented as specified**
1. **Live-verify v2 response item fields** (trade/activity/position item keys) against a real response at implementation start; the user-verified block pins *params* and the envelope, not item schemas. Record in `docs/API_INVENTORY.md`.
2. **Secret-rejection tests must include the no-echo assertion** and the `label`-field mnemonic case — this is the only security-critical surface.
3. **No-network tests** for all five endpoints (fakes only), or CI will flake on upstream outages.
4. **Adapters for `/status` + `/calibration` + `/signals`** — without them the Overview/Prove tabs render blanks against a real backend.

**P1 — strong hardening (recommended before demo)**
- TTL cache + Retry-After verification against a live 429; optional in-memory scan dedupe for repeated slugs; SQLite WAL; Auth0 gating for write routes; `docs/API_INVENTORY.md` v2 migration notes for the Oct 24 sunset.

**P2 — later**
- Promote loop helpers to `app/analysis.py`; extension-side repo/mini-RFC; vitest for frontend logic; remove the stale duplicate safety skill file.

**Rollout:** purely additive routes + one additive field + `CREATE TABLE IF NOT EXISTS`; deploy = normal image rebuild; no data migration. **Rollback:** revert the commit — the extra table and appended event types are inert for old code. **Observability:** every scan/validation/wallet action lands in the append-only audit log with model version, source, and (for rejects) masked diagnostics. **Unknowns:** v2 item schemas (P0-1), public RPC rate limits, real extension payload fidelity (an external repo we cannot see), whether the team wants Auth0 before demo.

---

## 12. What we will NOT touch

- `backend/app/loop.py`, `policy/gate.py`, `execution/paper.py`, `jev/client.py`, `scraper/*`, `playtest/*` — **zero edits** (helpers imported, not modified).
- Existing route behavior/shapes (only `/status` gains an additive field); `state.json`; the `events` schema; `killswitch.json`.
- No new dependencies (no keccak → no EIP-55 verification; no npm additions); no Dockerfile/compose/CI changes; no Auth0 wiring; no extension code.
- No order/signature/private-key anything; no new secrets; no raw `page_state` storage; no mock numbers added to the UI.
- Existing six dashboard tabs, pages, and SAMPLE-mode data stay as-is.

---

## 13. Phase 2 implementation order (after separate approval)

1. Deps-first: config additions, audit readers, `wallets.py`, v2 client methods, Polygon RPC client (+ unit tests).
2. `scan.py` + `schemas.py` + routes in `server.py` (+ endpoint tests per §9).
3. Docs + `.env.example` + README env table.
4. Frontend client/adapters → badge → Signals upgrade → Marketplaces/Wallets/Prove → build + manual checklist.
5. Full verify: `python -m pytest -q`, `npm run build`, offline loop sanity.

**Estimated review surface:** ~6 new backend files, 4 changed backend files (3 additive + 1 settings), 3 new frontend pages + 1 component, 7 changed frontend files (mostly wiring), tests, docs.

---
---

# RFC-003 — Market Guesser MVP (prediction-only)

| | |
|---|---|
| **RFC** | RFC-003 |
| **Status** | APPROVED — Part B in progress |
| **Depends on** | RFC-001 (merged: scan, wallets, proof surfaces) |
| **Repo / branch** | `D:\hunchfall` @ `main` |
| **Part A deliverable** | This section. **No implementation code in Part A.** |
| **Part B (after approval)** | Additive implementation + tests + docs; PR against `main`; **stop without merging.** |

> **APPROVED (2026-09-30):** Part B is in progress on `feat/rfc-003-market-guesser`. Everything below is the approved specification of what is being built; the plan text is kept as the record of what was agreed.

---

## 1. Summary

Given a Polymarket market, hunchfall guesses where it goes: **P(YES), direction, confidence** — as a paper prediction only. The simplest useful thing: no gate, no sizing, no fills; the loop, the deterministic gate, and the paper engine are untouched.

Four additive routes on the existing FastAPI app:

| Route | Purpose |
|---|---|
| `POST /predict` | `{market_slug \| condition_id}` → build a compact snapshot from official APIs, ask Jev for P(YES), return a typed prediction (or an honest abstention). |
| `GET /predict/demo` | Runs the whole pipeline on one pinned market; falls back to a committed canned snapshot. Frontend works with zero setup. **Never persists.** |
| `GET /predict/accuracy` | Derived-on-read calibration ledger: Brier vs market vs always-0.5, Brier skill scores, direction accuracy, abstention rate, mock/live split. |
| `POST /predict/{prediction_id}/resolve` | Records the realized `YES \| NO` outcome for one logged prediction (manual settlement; Gamma auto-settle is P1). |

Two new backend modules (`app/predict.py` for the guess, `app/social.py` for the social-media analyzer) plus one additive Data API method, one frontend tab, and no new dependencies. Mock mode works end-to-end without `JEV_API_KEY`; keys land later without a code change.

Polymarket lists **social-outcome markets** (post counts, engagement, account behavior). For those, the social-media analyzer **is the data source** — keyless Bluesky Jetstream + Reddit + RSS — not generic sentiment. X has no free tier, so X-targeted chatter is computed from cross-platform signals and labelled a **proxy**. The analyzer's output is a feature in the snapshot, never the decision.

---

## 2. Invariants (extend RFC-001 §2; mechanically tested)

1. **Prediction only.** `app/predict.py` and `app/social.py` never import `PaperEngine`, `app.execution`, or `app.loop`; the prediction path has no fill path and writes no `fill` events. The RFC-001 AST guard (`test_scan_prediction_only.py`) is extended to parse both modules — identifiers, imports, and runtime attributes.
2. **No scans, no loop runs.** Part B verification never runs `python -m app.loop`; CI stays exactly `pytest -q` (backend) + `npm run build` (frontend).
3. **Official APIs + keyless public social sources only.** Gamma (`/markets/slug/…`, market meta + `volume24hr`), CLOB market-data only (`/book`, `/midpoint`, …), Data API v2 (`/v2/trades`); the social-media analyzer reads keyless Bluesky **Jetstream** (websocket — `websockets` is already a dependency), Reddit public JSON, and RSS (existing `app/scraper/news.py` functions, imported read-only). **X is excluded** — no free tier — and X-targeted chatter is labelled a proxy. No other data source.
4. **Mock is labeled.** The mock path returns `model.mock=true`, `model.version="jev-mock"`, `model.note="MOCK — not a real model"`; `/predict/accuracy` excludes mock predictions by default; no win-rate claim is ever derived from mock runs.
5. **The badge.** Every prediction surface carries **“paper prediction · no trade placed”**; `/predict*` responses carry `label` + `trade_placed:false`.
6. **No wallet, no keys, no secrets.** `/predict` never touches wallets or RPC, never accepts/stores/echoes secrets, and never logs request bodies.
7. **Tests.** 1 happy-path + 2 negative tests per route, zero network (offline fakes injected by monkeypatch, mirrors the RFC-001 harness), plus unit tests for feature math and the ledger.
8. **Social output is a feature, not a signal.** Analyzer numbers enter the snapshot and the UI only: they can never set direction or abstention, never appear in `reasons` as outcome claims, and X-targeted runs always carry `proxy: true` plus the reason.

---

## 3. Tape contract correction (supersedes RFC-001 §3, D9 — for the predictor only)

**Verified live 2026-09-30 by the reviewer:** `GET /v2/trades?condition=<condition_id>` works and trades rows carry **no `usdc_size`**. USD notional per trade is **`size × price`**.

Consequences:

- The predictor tape uses the new `DataApiClient.get_trades_v2(condition=…, limit=…)` → `_get_v2("/v2/trades", …)` (same envelope unwrap, no `offset`, Retry-After ≤2 retries/≤5 s).
- **USD = `size × price` is the single definition** — if `usdc_size` ever appears on these rows it is ignored; a test pins this.
- RFC-001's extension-scan tape keeps using `/v2/activity?type=TRADE` (untouched); only the predictor adopts `/v2/trades`, where per-trade price/size/side/timestamp are needed for VWAP/momentum.
- Item keys are still treated defensively (`price`, `size`, `side`, `ts ∈ {timestamp, matchTime, match_time}`), and **Part B starts with a live-check** recording the exact key set in `docs/API_INVENTORY.md`; the fixture keys are pinned by tests. The inventory's “`/v2/trades` query params are NOT verified” note is corrected in Part B.

---

## 4. Pipeline (Part B shape)

```
POST /predict {market_slug | condition_id}
  → resolve_market()            Gamma only; slug path verified, condition_id path P0 live-check
  → CLOB book + midpoint        best bid/ask, spread, top-N depth, last_trade_price   (required)
  → Data API v2 /v2/trades      tape rows; USD = size × price                          (feature)
  → social pulse                Jetstream window + Reddit + RSS; shared 4 s deadline   (feature)
  → feature build               deterministic; canonical JSON → sha256                  (pure)
  → model                       Jev decide() × N → median p_yes, disagreement  |  MOCK
  → decision policy             edge, direction, abstention, confidence, reasons       (pure)
  → audit: predict event        append-only ledger                                      
  → 200 prediction
```

Rules:

- Gamma + CLOB are **required**: failure → 502 `upstream_error` naming the source, nothing persisted (fail closed).
- Tape and the social pulse are **features**: unavailable → omitted, listed in `snapshot.missing`, and named in `reasons`; the prediction is still returned. The social pulse runs **only for markets whose subject is social activity** (§5.1) — never as generic sentiment for every market.
- One snapshot per request; `snapshot_ts` is the server time of the snapshot. No caching in MVP.
- Nothing here reads `state.json`, the gate, exposure, or bankroll — a prediction is information, not a trade intent.

---

## 5. Compact snapshot & features (deterministic)

Computed from the upstream payloads; nulls are omitted rather than guessed. All math is pinned by hand-computed unit tests.

| Feature | Definition | Null policy |
|---|---|---|
| `market_mid` | CLOB midpoint of the YES token (outcomes/`clobTokenIds` position-aligned; index 0 = YES) | required |
| `best_bid`, `best_ask` | top of the YES book | required |
| `spread_cents` | `(ask − bid) × 100` | required |
| `bid_depth_usd`, `ask_depth_usd` | Σ `price × size` over top `PREDICT_BOOK_LEVELS` (default 5) | 0 when side empty |
| `imbalance` | `(bid_depth − ask_depth) / (bid_depth + ask_depth)` ∈ [−1, 1] | 0 when both 0 |
| `last_trade_price` | from the book payload when present | omitted |
| `tape.count` | rows returned | 0 allowed |
| `buy_usd`, `sell_usd` | Σ `size × price` per side | 0 |
| `flow_imbalance` | `(buy − sell) / (buy + sell)` | 0 when total 0 |
| `vwap` | Σ`size·price` / Σ`size` | omitted when no rows |
| `momentum_15m`, `momentum_60m` | VWAP(last window) − VWAP(prior window), in probability points | omitted when either window empty |
| `volume_accel` | `usd(last 15m) / (usd(15m–2h15m) / 8)` (equal-length windows) | omitted when baseline 0 |
| `volume24hr_usd` | Gamma `volume24hr` (aliases `volume24Hr`, `volume_24hr`) | omitted |
| `hours_to_resolution` | local date math on Gamma `endDate` | omitted |
| `social.relevance`, `social.proxy` | `direct` (target observable on Bluesky) \| `proxy` (X-targeted: cross-platform chatter only, `proxy: true` + reason) \| `none` (pulse skipped) | `none` when the market is not social-outcome related |
| `social.posts_window`, `social.engagement_window`, `social.velocity`, `social.chatter_score`, `social.top_item` | Jetstream window counts (posts; like/repost events whose subject is in the collected set), recent-vs-prior velocity, 0..1 composite, best item `{platform, text ≤200, url, ts}` | omitted when no platform returned data |
| `social.platforms_ok`, `social.missing[]` | which sources answered inside the shared deadline | always shown |
| `base_rate_anchor` | **= `market_mid`**, surfaced to the model as “market-implied base rate” | required |

Snapshot text handed to Jev (compact, ≤2 KB, control chars stripped):

```
Market: <question>
Market-implied base rate (YES mid): 0.550
Book: bid 0.54 / ask 0.56 · spread 2.0¢ · depth $5.2k bid / $3.9k ask · imbalance +0.15
Tape (100 trades): buy $18,400 / sell $11,700 · flow +0.22 · vwap 0.551
Momentum: 15m +0.004 · 60m +0.015 · volume_accel 1.8x
Resolution in 2201.4h · 24h volume $48,210
Social (bluesky 3s window · direct): 41 posts · 12 engagements · velocity 1.4x
Chatter (reddit + rss · proxy for X): 9 mentions · top: "<title>" (6.2h)
```

### 5.1 Social-media analyzer (social-outcome markets)

Polymarket lists markets whose resolution depends on social-media activity. For those markets this analyzer **is the data source** — it answers “is the subject posting / being talked about right now”, not “is the mood good”.

- **Sources (all keyless):** Bluesky **Jetstream** v2 websocket (same JSON payload as v1; live tail filterable by collections/DIDs — v2 live path `/xrpc/network.bsky.jetstream.subscribeEvents`, v1-style `wss://…/subscribe` URLs still served; the exact public instance URL is pinned at the Part B live-check), Reddit public JSON (`/search.json` with a custom User-Agent — the OAuth path in `scraper/reddit.py` stays optional and untouched), and RSS (`scraper/news.py`, read-only). **X is excluded**; X-targeted markets get a labelled proxy instead.
- **Relevance modes:** `direct` when the target itself is observable on Bluesky (count its posts/engagement in the window; DID filter when resolvable); `proxy` when the market is X-centric (cross-platform chatter only, `proxy: true` + reason in the state); `none` → the pulse is skipped entirely and the feature is absent. The classifier is a pinned keyword/entity heuristic; expanding it is a data change, not a code change.
- **Output:** one `social` feature block (§5 table) consumed by the model as context and shown in the UI. It never sets direction/abstention, never produces outcome claims in `reasons`, and its numbers are never presented as the platform's own metric when `proxy: true`.
- **Budget:** one shared deadline (`SOCIAL_DEADLINE_SEC`, default 4 s) across sources; a slow source is dropped, not awaited. The Jetstream window is bounded (`SOCIAL_JETSTREAM_WINDOW_SEC`, default 3 s, `SOCIAL_MAX_EVENTS` cap, connect → count → close; no long-lived subscription in MVP). Counts are window facts, not reproducible across calls; the math and response shapes are pinned by tests.

**6.1 Model runs (ensemble).** `n = clamp(PREDICT_ENSEMBLE_N, 1, 5)` (default 3) calls to the same model; `p_yes = median(p_i)`; `disagreement = max(p_i) − min(p_i)` (0 in mock — the mock is deterministic, so all runs are identical; documented, not hidden). Per-run raw values are not returned; the audit stores model version + ensemble stats only.

**6.2 Model framing (MVP).** Reuse `JevClient.decide()` unchanged — zero edits to `jev/client.py`. `DecisionState.market_question` = the market question; `news_summary` = the top social item or RSS headline (or “no fresh social data”); `extra_context` = the feature block above, including the social pulse with its relevance/proxy labels; `yes_price` = `market_mid`. The returned `p_true` (noul) is used directly as **P(YES)**. A dedicated P(YES) question set is P1 and would require loop regression tests first.

**6.3 Decision fields.**

- `edge_vs_market = round(p_yes − market_price, 4)` (signed).
- `direction = "YES"` when `edge ≥ threshold`; `"NO"` when `edge ≤ −threshold`; else `"ABSTAIN"`, with `threshold = PREDICT_ABSTAIN_EDGE` (default **0.10**).
- `abstained = direction == "ABSTAIN"` — no guess is surfaced when the model adds less than 10% over the market.
- `confidence = round(min(1.0, abs(2·p_yes − 1) × (1 − min(1, disagreement))), 4)` — distance from a coin flip, discounted by ensemble spread. It is **not a probability** and the docs say so.
- `reasons[]` — typed strings only (values from snapshot/decision), ≥1 always; abstention adds its threshold reason, e.g. `"abstained: |edge 0.041| < threshold 0.100"`. Never model prose.
- Rounding: 4 dp for probabilities/edges/confidence, 2 dp for USD.

**6.4 Demo determinism.** The canned demo snapshot is chosen so that the deterministic mock yields a **non-abstained** guess (pinned by a test), so the dashboard always demonstrates a real result card; `snapshot_source: "live" | "canned"` states which one served. Demo never writes to the ledger.

---

## 7. Endpoint contracts

Error envelope everywhere: `{"detail": {"code": "…", "message": "…", "source"?: "gamma|clob|data-api|jev"}}` (RFC-001 §6). All responses carry `mode:"PAPER"`, `label:"paper prediction · no trade placed"`, `trade_placed:false`.

### 7.1 `POST /predict`

Request (`PredictRequest`, `extra="forbid"`): **exactly one** of

| Field | Type / rules |
|---|---|
| `market_slug` | str, 1–160, pattern `^[A-Za-z0-9._~-]+$` |
| `condition_id` | str, `^0x[0-9a-fA-F]{64}$` |

Missing/extra/unknown fields → 422 (pydantic). Neither identifier → 422; both → 422.

```json
// 200 — the seven requested fields are exactly prediction.*
{
  "mode": "PAPER",
  "market": {
    "question": "Will BTC hit $100k in 2026?",
    "slug": "will-btc-hit-100k-in-2026",
    "condition_id": "0x…",
    "yes_token_id": "…",
    "no_token_id": "…",
    "end_date": "2026-12-31T23:59:59Z",
    "hours_to_resolution": 2201.4,
    "volume24hr_usd": 48210.5,
    "closed": false
  },
  "prediction": {
    "prediction_id": "0e2b8f6c-…",
    "p_yes": 0.67,
    "direction": "YES",
    "confidence": 0.34,
    "edge_vs_market": 0.12,
    "abstained": false,
    "reasons": [
      "book_imbalance +0.15 (bids dominate top 5 levels)",
      "flow_imbalance +0.22 over 100 trades ($18,400 buy / $11,700 sell)",
      "momentum_60m +0.015",
      "volume_accel 1.8x (last 15m vs prior 2h rate)",
      "social pulse: 41 posts / 12 engagements on bluesky (direct, 3s window)",
      "base_rate anchor: market-implied 0.55",
      "edge +0.120 ≥ threshold 0.100 → YES"
    ],
    "snapshot_ts": "2026-09-30T16:40:00Z"
  },
  "market_price": 0.55,
  "abstain_threshold": 0.10,
  "model": {
    "mode": "mock", "mock": true, "version": "jev-mock",
    "ensemble_n": 3, "disagreement": 0.0,
    "note": "MOCK — not a real model"
  },
  "snapshot": {
    "sha256": "…", "missing": [],
    "features": {
      "market_mid": 0.55, "best_bid": 0.54, "best_ask": 0.56, "spread_cents": 2.0,
      "bid_depth_usd": 5210.5, "ask_depth_usd": 3871.2, "imbalance": 0.147,
      "tape": {"count": 100, "buy_usd": 18400.0, "sell_usd": 11700.0,
               "flow_imbalance": 0.2227, "vwap": 0.5512,
               "momentum_15m": 0.004, "momentum_60m": 0.015, "volume_accel": 1.8},
      "volume24hr_usd": 48210.5, "hours_to_resolution": 2201.4,
      "social": {"relevance": "direct", "proxy": false,
                 "platforms_ok": ["bluesky", "reddit", "rss"], "missing": [],
                 "posts_window": 41, "engagement_window": 12, "velocity": 1.4,
                 "chatter_score": 0.58,
                 "top_item": {"platform": "bluesky", "text": "…", "ts": "…"}}
    }
  },
  "audit_event_id": 187,
  "label": "paper prediction · no trade placed",
  "trade_placed": false
}
```

Abstained responses have the same shape with `direction:"ABSTAIN"`, `abstained:true`, and the threshold reason in `reasons`.

| Status | Code | When |
|---|---|---|
| 200 | — | Prediction logged (`trade_placed:false`) |
| 404 | `market_not_found` | Gamma has no market for the slug/condition |
| 409 | `market_not_tradeable` | `closed:true`, or no two-sided token pair |
| 422 | pydantic | Neither/both identifiers, malformed values, unknown fields |
| 502 | `upstream_error` | Gamma/CLOB/Data API/Jev failure (source named); **no predict event persisted** |

**Persisted:** one `predict` event per successful guess (§8). Upstream failures persist only `stage_error`, like RFC-001.

### 7.2 `GET /predict/demo`

No params. **Always 200**, never persists. Runs the pinned market (`PREDICT_DEMO_SLUG`) through the full pipeline; on any upstream failure (or when `PREDICT_DEMO_LIVE=false`) serves the committed canned snapshot.

```json
{
  "mode": "PAPER",
  "demo": true,
  "snapshot_source": "canned",
  "market": { "…": "same block as /predict" },
  "prediction": { "…": "same block; canned snapshot is pinned to a non-abstained mock guess", "snapshot_ts": "2026-09-30T16:40:00Z" },
  "market_price": 0.55,
  "abstain_threshold": 0.10,
  "model": {"mode": "mock", "mock": true, "version": "jev-mock", "ensemble_n": 1, "disagreement": 0.0, "note": "MOCK — not a real model"},
  "snapshot": {"sha256": "…", "missing": [], "features": {"…": "same shape"}},
  "audit_event_id": null,
  "label": "paper prediction · no trade placed",
  "trade_placed": false
}
```

### 7.3 `GET /predict/accuracy`

Query: `include_mock=false` (default), `limit=500` (predictions scanned). Always 200; empty ledger → zeros with `n_logged: 0` (honest empty, nothing invented). Definitions in §8.

```json
{
  "mode": "PAPER",
  "as_of": "2026-09-30T16:45:00Z",
  "include_mock": false,
  "n_logged": 12, "n_resolved": 7, "n_abstained": 3, "n_guessed": 9, "n_resolved_guessed": 6,
  "abstention_rate": 0.25, "mean_abs_edge": 0.14,
  "brier": {"model": 0.1132, "market": 0.1604, "baseline_0_5": 0.25,
            "skill_vs_market": 0.2943, "skill_vs_baseline": 0.5472},
  "brier_guessed": {"model": 0.0987, "market": 0.1511, "n": 6},
  "direction_accuracy": 0.8333,
  "bins": [{"lo": 0.0, "hi": 0.1, "n": 0, "mean_p": null, "event_rate": null}, "… (10 bins)"],
  "mock_split": {"live": {"n_logged": 12, "n_resolved": 7}, "mock": {"n_logged": 0, "n_resolved": 0}},
  "label": "paper predictions only · no trade placed"
}
```

### 7.4 `POST /predict/{prediction_id}/resolve`

Request: `{"outcome": "YES" | "NO"}` (`extra="forbid"`). Appends one `predict_resolved` event; history is never mutated.

```json
// 200
{
  "mode": "PAPER",
  "prediction_id": "0e2b8f6c-…",
  "outcome": "YES",
  "resolved_at": "2026-09-30T17:00:00Z",
  "prediction": {"p_yes": 0.67, "direction": "YES", "confidence": 0.34, "abstained": false},
  "brier": {"model": 0.1089, "market": 0.2025, "baseline_0_5": 0.25,
            "skill_vs_market": 0.4622, "skill_vs_baseline": 0.5644},
  "label": "paper prediction · no trade placed"
}
```

| Status | Code | When |
|---|---|---|
| 404 | `unknown_prediction` | No `predict` event has this `prediction_id` |
| 409 | `already_resolved` | A `predict_resolved` event exists (previous outcome returned) |
| 422 | pydantic | `outcome` not YES/NO |

---

## 8. Calibration ledger & metric definitions

Append-only audit events (no new tables, no `state.json` writes, no mutation):

| Event | Payload keys |
|---|---|
| `predict` | `prediction_id` (uuid4), `condition_id`, `slug`, `question`, `p_yes`, `market_price`, `edge_vs_market`, `direction`, `confidence`, `abstained`, `abstain_threshold`, `model{mode,mock,version,ensemble_n,disagreement}`, `snapshot_sha256`, `snapshot_ts`, `features{…}`, `missing[]`, `social{relevance,proxy,platforms_ok,missing}`, `reasons[]` |
| `predict_resolved` | `prediction_id`, `outcome` (YES/NO), `resolved_at`, `source` (`manual`; `gamma` reserved for P1 auto-settle) |
| `stage_error` | `stage`, `error` — upstream failures; nothing else persisted |

Derived on read by `GET /predict/accuracy` (recomputed per request, so the math is auditable from events alone):

- `outcome = 1` for YES, `0` for NO.
- `brier.model = mean((p_yes − outcome)²)` over resolved predictions; `brier.market` uses `market_price`; `baseline_0_5 = 0.25` exactly.
- `skill_vs_market = 1 − model/market` (null when `market` is 0); `skill_vs_baseline = 1 − model/0.25`; 4 dp.
- `brier_guessed` repeats the model+market pair over resolved **non-abstained** predictions, so the abstention filter's effect is visible, not hidden.
- `direction_accuracy` over resolved non-abstained: `YES && outcome=1` or `NO && outcome=0`.
- `abstention_rate = n_abstained/n_logged`; `mean_abs_edge = mean(|edge_vs_market|)` over logged guessed predictions.
- `bins`: 10 equal-width bins over `p_yes ∈ [0,1]`; each `{lo, hi, n, mean_p, event_rate}`; empty bin → nulls.
- `mock_split` always reported; headline metrics honor `include_mock` (default **false**) so mock runs can never inflate or deflate claimed accuracy.
- Tiny-n honesty: every response carries its n; the UI never renders a rate without it.

---

## 9. Frontend contract (Part B)

| Call | When | Use |
|---|---|---|
| `GET /predict/demo` | On Predict tab open (zero setup) | Renders a result card immediately; `MOCK` chip + `snapshot_source` note |
| `POST /predict` `{market_slug}` | On submit | Result card, or the abstention card |
| `GET /predict/accuracy` | Tab open and after each resolve | Brier tiles + mock/live split |
| `POST /predict/{prediction_id}/resolve` `{outcome}` | YES/NO buttons on a logged result | Refresh accuracy |

UI states: idle, loading, result, **abstained** (copy: `"no guess — edge 0.041 vs threshold 0.100"`), error (render `ApiError.detail` verbatim). The result card shows `p_yes` vs `market_price` on one bar, a direction chip (YES/NO/ABSTAIN), confidence, edge, the reasons list, and the badge. **No synthetic sample data for this surface** — `/predict/demo` is the sample path; SAMPLE mode renders honest empties (RFC-001 rule).

TS interfaces + `apiPostJson` calls go in `frontend/src/api/client.ts`; no new dependencies.

---

## 10. Files & settings (Part B change list)

**Backend — new**

| File | Contents |
|---|---|
| `backend/app/predict.py` | `PredictService`: `resolve_market()`, snapshot/feature builder, `PredictorModel` seam (Jev wrapper + existing MOCK reuse), decision policy, ledger reads; error classes; **no `app.loop` / `app.execution` imports** |
| `backend/tests/test_api_predict.py` | Route matrix (§11) |
| `backend/tests/test_predict_features.py` | Hand-computed feature math + parser pins |
| `backend/tests/test_predict_ledger.py` | Brier/skill/bins/direction math + mock split |
| `backend/app/social.py` | `SocialAnalyzer`: social-market classifier, bounded Jetstream window (websockets), Reddit JSON + RSS collectors, shared deadline, `chatter_score`/velocity math, proxy labelling; **no `app.loop` / `app.execution` imports** |
| `backend/tests/test_social_analyzer.py` | Offline fixtures: fake Jetstream event stream, fake Reddit JSON, fake RSS; classifier, math, deadline/omission, proxy labelling |

**Backend — changed (all additive):** `app/polymarket/data_api.py` (`get_trades_v2`), `app/api/server.py` (4 routes, `app.state.predict`), `app/api/schemas.py` (`PredictRequest`, `ResolveRequest`), `app/config.py` (settings below), `app/memory/audit.py` (read helpers `predictions` / `prediction_resolutions`), `tests/test_data_api_v2.py` (+ `/v2/trades` unwrap, no-`usdc_size`, USD formula), `tests/test_scan_prediction_only.py` (guard extended to `app.predict` and `app.social`), `backend/.env.example`, `README.md` env table, `docs/API_INVENTORY.md`, `docs/USER_WORKFLOW.md` (optional).

**Settings (additive, `.env.example` only — no secrets)**

| Setting | Default | Meaning |
|---|---|---|
| `PREDICT_ABSTAIN_EDGE` | `0.10` | `\|edge\|` below this → ABSTAIN |
| `PREDICT_ENSEMBLE_N` | `3` | Jev runs per prediction, clamped 1–5 |
| `PREDICT_TAPE_LIMIT` | `100` | `/v2/trades` page size |
| `PREDICT_BOOK_LEVELS` | `5` | Depth levels per book side |
| `SOCIAL_ENABLED` | `true` | Social pulse for social-outcome markets |
| `SOCIAL_DEADLINE_SEC` | `4.0` | Shared deadline across sources; drop the slow ones |
| `SOCIAL_JETSTREAM_URL` | pinned in Part B | Jetstream websocket URL (live-checked) |
| `SOCIAL_JETSTREAM_WINDOW_SEC` | `3.0` | Bounded connect → count → close window |
| `SOCIAL_MAX_EVENTS` | `2000` | Hard event cap per window |
| `SOCIAL_REDDIT_ENABLED` | `true` | Keyless Reddit JSON (custom User-Agent) |
| `SOCIAL_RSS_MAX_ITEMS` | `10` | RSS items per pulse |
| `PREDICT_DEMO_SLUG` | pinned in Part B | Demo market (live-verified, long-dated) |
| `PREDICT_DEMO_LIVE` | `true` | `false` → demo always serves the canned snapshot |

**Frontend — new:** `frontend/src/pages/Predict.tsx`. **Changed (additive):** `api/client.ts`, `components/Nav.tsx`, `App.tsx`, `index.css`.

---

## 11. Test plan

| Route | Happy | Negative 1 | Negative 2 |
|---|---|---|---|
| `POST /predict` | Fake Gamma + CLOB + `/v2/trades` + fake Jev → 200; exact `prediction.*` fields; USD = `size × price` from a fixture **with no `usdc_size`**; `predict` event persisted; `audit.query("fill") == []` | Unknown slug → 404 `market_not_found`; nothing persisted | Empty CLOB book → 502 `upstream_error` `source:"clob"`; `stage_error` recorded; no `predict` event |
| `GET /predict/demo` | All upstreams fail → 200, `snapshot_source:"canned"`, pinned non-abstained guess | Live path succeeds → 200, `snapshot_source:"live"` | Assert **no** `predict` event on either path |
| `GET /predict/accuracy` | Seed predictions + resolutions → hand-computed Brier/skill/bins | Empty ledger → zeros, `n_logged:0` | Mock-only ledger with `include_mock=false` → zeros; `include_mock=true` → mock counted |
| `POST /predict/{id}/resolve` | Logged prediction → 200; `predict_resolved` event; accuracy recomputed | Unknown id → 404 | Duplicate → 409 with the previous outcome |

**Unit:** feature math (imbalance, flow, VWAP, momentum windows, volume_accel, null policies); social analyzer (classifier `direct`/`proxy`/`none`, Jetstream window counts, engagement intersection, velocity, deadline drop, proxy labels, `chatter_score` formula); abstention boundary (`edge == threshold` guesses, below abstains); confidence formula; deterministic mock (same snapshot → same `p_yes`).

**Guard/safety:** `test_scan_prediction_only.py` extended to parse `app.predict` and `app.social` — fails on any `PaperEngine` / `app.execution` / `app.loop` identifier, import, or module attribute; a runtime test asserts `/predict` never constructs the paper engine (patch an exploding sentinel). Zero network: every upstream (Gamma/CLOB/Data API/Jev/Jetstream/Reddit/RSS) is monkeypatched.

---

## 12. Risks & mitigations

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| R1 | `condition_id` resolution surface unverified | M | M | P0 live-check at Part B start; single `resolve_market()` seam; the slug path is verified regardless |
| R2 | `/v2/trades` row key drift | M | M | Defensive aliases + live-check; key set recorded in `docs/API_INVENTORY.md`; USD formula pinned by test |
| R3 | Social-source latency/entropy (firehose volume, Reddit 403/429) | M | L | One shared 4 s deadline; Jetstream window capped and closed; a slow/failing source is dropped and listed in `social.missing`; never blocks the response |
| R4 | Mock mistaken for a real model | M | H | `model.mock` + note + UI MOCK chip + `include_mock=false` default + `mock_split` |
| R5 | Ensemble cost/latency | L | L | N ≤ 5 (default 3), ~250 ms p50 per Jev call |
| R6 | Tiny-n accuracy over-claims | M | M | Every metric carries n; no rate is shown without it |
| R7 | Resolution never happens (far-dated markets) | H | M | Manual resolve is the MVP path; P1 Gamma auto-settle with a confirmed winner (RFC-001 zero-price gotcha) |
| R8 | Scope creep into gate/loop | L | H | AST guard + §13 do-not-touch list + PR review |
| R9 | Proxy chatter mistaken for X's own metrics | M | H | X-targeted runs carry `proxy:true` + reason in the state; UI and `reasons` label it a proxy; never presented as platform metrics |
| R10 | Jetstream v1→v2 endpoint/payload drift | M | M | Live-check at Part B start (v2 serves the v1 JSON payload); URL/path pinned in config; event fixtures pinned by tests |

---

## 13. What we will NOT touch

- `backend/app/loop.py`, `policy/gate.py`, `execution/paper.py`, `scan.py`, `wallets.py`, `jev/client.py`, `scraper/*`, `playtest/*` — **zero edits**. `scraper` functions are imported read-only for RSS (`reddit.py` OAuth stays optional and unused by default).
- Existing route behavior/shapes (only new routes are mounted); `state.json`; the `events` schema; `killswitch.json`.
- No new Python/npm dependencies (httpx/feedparser already present); no CI changes; no extension code; no Auth0.
- No order/signature/private-key anything; no request-body logging; no mock win-rate claims.
- No `python -m app.loop` runs during Part B verification.

---

## 14. Part B implementation order (after approval)

1. Config + `get_trades_v2` + live-checks (`/v2/trades` keys, `condition_id` lookup, Jetstream URL/payload, Reddit JSON behavior); record in `docs/API_INVENTORY.md`; unit tests.
2. `app/predict.py` (features → model → policy → ledger) + `app/social.py` (classifier → pulse) + unit tests.
3. `schemas.py` + routes in `server.py` + endpoint tests (1 happy + 2 negative each) + guard extension.
4. Docs (inventory correction incl. the social-sources table, env table, README, workflow note).
5. Frontend Predict tab + client + nav + build.
6. Full verify: `cd backend && python -m pytest -q` (all green); `python -c "import app.predict, app.api.server, app.polymarket.data_api"`; `cd frontend && npm run build` (locally via `./node_modules/.bin/tsc -b && ./node_modules/.bin/vite build`).
7. Open a PR against `main` with evidence. **Stop — no merge until instructed.**

**Estimated review surface:** ~2 new backend modules, 5 changed backend files (all additive), 4 new test files + 2 extended, 1 new frontend page, 4 changed frontend files, docs.

---

**STOP — Part A ends here. Awaiting approval before any code.**
