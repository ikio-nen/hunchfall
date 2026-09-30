# RFC.md — Extension Scan, Signal Validation, Watch-Only Wallets, Marketplaces & Proof Dashboard

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
