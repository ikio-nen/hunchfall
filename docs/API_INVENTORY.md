# hunchfall — API Inventory

> Verified endpoint table (research pass, 2026-09-30). `status` = `verified`
> (endpoint path, auth, and rate limit confirmed) or `live-check` (verify at
> build time against a real response before depending on payload field names).

Columns: endpoint | method | auth? | rate limit | used for | status.

## Gamma Data API (base `https://gamma-api.polymarket.com`)

| Endpoint | Method | Auth? | Rate limit | Used for | Status |
|---|---|---|---|---|---|
| `/events` | GET | No (public) | 500 req / 10s | story -> event matching (fallback) | verified |
| `/events/{id}` | GET | No (public) | 500 req / 10s | event detail incl. nested markets | verified |
| `/events/slug/{slug}` | GET | No (public) | 500 req / 10s | event detail by slug | verified |
| `/markets` | GET | No (public) | 300 req / 10s | market listing (keyset pagination: `next_cursor` -> `after_cursor`) | verified |
| `/markets/{id}` | GET | No (public) | 300 req / 10s | market detail: question, outcomes, outcomePrices, clobTokenIds, volume, liquidity, endDate, closed, resolutionSource | verified |
| `/markets/slug/{slug}` | GET | No (public) | 300 req / 10s | market detail by slug | verified |
| `/public-search` | GET | No (public) | 350 req / 10s | **primary story -> market matcher** | verified |
| `/tags` | GET | No (public) | general 4000 / 10s | fee-category mapping surface | verified |
| `/series` | GET | No (public) | general 4000 / 10s | series browsing | verified |
| `/sports` | GET | No (public) | general 4000 / 10s | sports markets | verified |

Gotchas (verified):
- `outcomePrices` arrives as a **JSON string** — must be parsed (`parse_outcome_prices`).
- `clobTokenIds` arrives as a **JSON-encoded array string**
  (`'["3233…", "2565…"]'`), not a bare comma-separated list (live-checked
  2026-10-01; splitting on `,` sent CLOB ids wrapped in `["` … `"]` and every
  book came back empty). `parse_token_ids` accepts both forms.
- **A condition id is not a market id.** `GET /markets/0x<64hex>` is rejected
  (`{"type": "validation error", "error": "id is invalid"}`); resolve a raw
  condition id with `GET /markets?condition_ids=<id>`
  (`get_market_by_condition_id`, live-checked 2026-10-01). The filter matches
  **case-sensitively** (lowercase first) and returns **open markets only** — a
  *resolved* market needs `closed=true`, or it is indistinguishable from an
  unknown id.
- Resolved markets may report `outcomePrices` as `["0","0"]` — **validate winner
  fields before trusting**; the loop treats zeroed prices with no confirmed
  winner as a broken feed (veto `negrisk_sum_invalid`), never as a signal.

## CLOB API — market data only (base `https://clob.polymarket.com`)

| Endpoint | Method | Auth? | Rate limit | Used for | Status |
|---|---|---|---|---|---|
| `/price?token_id=X&side=BUY` | GET | No (public) | 1500 req / 10s | executable-side price per token | verified |
| `/midpoint?token_id=X` | GET | No (public) | 1500 req / 10s | mid price per token | verified |
| `/book?token_id=X` | GET | No (public) | 1500 req / 10s | order book (incl. `min_order_size`, `tick_size`, `last_trade_price`); spread, depth, book-walk levels | verified |
| `/spread?token_id=X` | GET | No (public) | 1500 req / 10s | spread payload | verified |
| `/prices-history` | GET | No (public) | 1000 req / 10s | price history per token | verified |
| `/tick-size?token_id=X` | GET | No (public) | 1500 req / 10s | min price increment | verified |
| `/books`, `/prices`, `/midpoints` (batched) | GET | No (public) | 500 req / 10s | batched market data | verified |

General CLOB limit: 9000 req / 10s.

## Data API (base `https://data-api.polymarket.com`)

| Endpoint | Method | Auth? | Rate limit | Used for | Status |
|---|---|---|---|---|---|
| `/v2/prices-history` | GET | No (public) | 200 req / 10s | price history v2 | verified |
| `/v2/activity` | GET | No (public) | (general) | extension-scan trade tape (`type=TRADE`) + wallet activity feed (`user`) | verified |
| `/v2/positions` | GET | No (public) | (general) | watch-only wallet open positions | verified |
| `/v2/trades` | GET | No (public) | (general) | RFC-003 predictor tape; USD = `size × price` | verified |

**v1 is retired and removed from the client (2026-10-24):** `GET /trades`,
`/holders`, and `/oi` no longer exist in `app/polymarket/data_api.py` — no dead
v1 surface ships. The loop's snapshot tape read moved from
`get_trades(token_id=…)` to `get_trades_v2(condition=<conditionId>)`, and
`get_holders` / `get_oi` were deleted (they had no callers).

### Data API v2 — verified params (2026-09-30, official openapi.json)

Global rules: responses are wrapped in `{"data": ..., "pagination": {"next_cursor": ...}}`;
**no `offset` param** (sending one returns 400); params accept snake_case and
camelCase; `429` carries `Retry-After`; a documented miss returns an empty
`data` array (valid zero-state); all public, no auth. **v1 retires 2026-10-24.**

| Endpoint | Verified params | Used for | Status |
|---|---|---|---|
| `/v2/activity` | `user` (required, EVM address); `type` (comma-separated, e.g. `TRADE`; `TIP` opt-in only); `condition` (≤20 ids; aliases `condition_id`/`conditionId`); `event_id` (≤20, mutually exclusive with `condition`); `side`; `start`/`end` (epoch s); `limit` (default 100, max 1000); `cursor`; `sort_direction`; `exclude_deposits_withdrawals` (default true) | extension-scan trade tape (`type=TRADE&condition=`) + wallet activity feed (`user`) | verified |
| `/v2/positions` | `user`; `status` (`OPEN`/`CLOSED`); `limit`; `cursor` | watch-only wallet open positions | verified |

Note: `/v2/trades` query params were **live-verified 2026-10-01**
(`condition` + `limit`; see below). A non-proxy-wallet `user` on `/v2/activity`
returns an empty `data` array; treat as "no activity", not an error.

### `/v2/trades` — RFC-003 predictor tape (live-verified 2026-10-01)

The RFC-003 predictor adopts `/v2/trades?condition=<condition_id>&limit=N`
(per-trade price/size/side/timestamp are needed for VWAP + momentum), where
**USD notional per trade is `size × price`** — the rows carry **no
`usdc_size`**. If `usdc_size` ever appears on these rows it is ignored; the
formula is pinned by a unit test. `/v2/activity?type=TRADE` stays the
RFC-001 extension-scan tape and is untouched.

**Live-check result: VERIFIED (2026-10-01, via the documented read-only fetch
proxy — direct egress to every Polymarket host still times out on the build
network; re-run `backend/scripts/live_check.py` without `--proxy` to reproduce
first-party).** Observed row key set (20 keys):

`proxy_wallet`, `side`, `token_id`, `condition_id`, `size`, `price`,
`timestamp`, `title`, `slug`, `icon`, `event_slug`, `outcome`,
`outcome_index`, `name`, `pseudonym`, `bio`, `profile_image`,
`profile_image_optimized`, `transaction_hash` — **no `usdc_size`**.

Field confirmations, and the bugs the check found:

* `price`/`size` are plain numbers (`0.974`, `100.0`), `side` is `BUY`/`SELL`,
  and `timestamp` is epoch **seconds** — the defensive aliases hold.
* **Both outcomes interleave in one feed**: 71 of the last 100 rows for one
  market were NO-token trades near 0.97 while the YES token traded at 0.029,
  so the unfiltered vwap read **0.7628 for a 2.85¢ market** and its "buy"
  flow was really the NO side's. `tape_features` now scopes every row to the
  YES `token_id` (`outcome_index`/`outcome` accepted as aliases; a row that
  identifies neither is dropped) — pinned by test.
* The same live pass found three more shape bugs in the read path
  (`clobTokenIds` is a JSON-array string; `/book` levels are objects with
  string numbers served **worst-first**) — see `docs/PLAYTESTING.md` §7.4.

## hunchfall backend routes — RFC-001 additions (2026-09-30)

| Route | Method | Purpose |
|---|---|---|
| `/extension/scan` | POST | Re-validate a page hint (Gamma → CLOB → Data API v2 → Jev → risk gate) and persist a paper prediction. Never places a fill. |
| `/signals/{id}/validate` | POST | Record a HIT/MISS verdict (append-only); 404 unknown, 409 duplicate; calibration derived on read. |
| `/wallets` | POST | Watch-only registry; anything resembling a private key or mnemonic → 400, never echoed/stored. |
| `/wallets/{address}/activity` | GET | Read-only activity (Data API v2 + public Polygon RPC balance/nonce); 404 unregistered; graceful `degraded` on partial outage. |
| `/marketplaces` | GET | Scans / hunches / validations grouped per marketplace. |

`GET /status` also gained an additive `equity_curve` field derived from
`cycle_end` audit events.

## hunchfall backend routes — RFC-003 additions (market guesser, prediction-only)

| Route | Method | Purpose |
|---|---|---|
| `/predict` | POST | `{market_slug \| condition_id}` → Gamma resolve → CLOB book → `/v2/trades` tape → social pulse (social-outcome markets only) → Jev ensemble → typed P(YES) prediction or an honest abstention. Never places a fill. |
| `/predict/demo` | GET | Runs the pinned demo market through the full pipeline; falls back to a committed canned snapshot. Always 200; **never persists**. |
| `/predict/accuracy` | GET | Derived-on-read calibration ledger: Brier (model vs market vs always-0.5), Brier skill, direction accuracy, abstention rate, mock/live split. |
| `/predict/{prediction_id}/resolve` | POST | Records the realised `YES \| NO` outcome for one logged prediction (append-only; manual settlement). |

All four are additive; the loop, the deterministic gate, and the paper engine
are untouched, and the prediction path never imports `PaperEngine`,
`app.execution`, or `app.loop` (enforced by the extended AST guard).

## Social sources — RFC-003 analyzer (Part B live-check, 2026-09-30)

| Source | Endpoint used | Auth? | Live-check result |
|---|---|---|---|
| Bluesky **Jetstream** (posts/engagements) | `wss://jetstream2.us-east.bsky.network/subscribe` | No (keyless) | **VERIFIED** — connects and streams; 3 events in a 4 s window |
| Bluesky public appview (handle/DID resolution) | `https://public.api.bsky.app/xrpc/app.bsky.actor.searchActors?q=` | No (keyless) | **VERIFIED** — `200` JSON (`actors[]`) |
| Reddit public JSON | `https://www.reddit.com/search.json?q=` (custom User-Agent) | No (keyless) | **NOT AVAILABLE** — `403` on every variant tried (custom UA, browser UA, `r/polymarket/hot.json`, and `oauth.reddit.com` without a token) |
| RSS | `app/scraper/news.py` (Google News + outlet feeds) | No (keyless) | verified (RFC-001, unchanged; imported read-only) |
| **X / Twitter** | — | — | **EXCLUDED BY DESIGN** (no free tier); X-centric markets get a labelled **proxy** |

**Jetstream payload (observed live):** top-level keys `did`, `time_us`,
`kind`, `commit`; `commit` carries `operation`, `collection` (e.g.
`app.bsky.feed.like`), `rkey`, and `record`. The RFC's assumed
`/xrpc/network.bsky.jetstream.subscribeEvents` suffix is **wrong for this
instance** — a plain HTTP GET and a websocket upgrade both return `404`;
the correct live path is `/subscribe`. The analyzer pins the working URL in
`SOCIAL_JETSTREAM_URL`, treats the payload defensively
(`kind`/`commit.collection`/`did`), and caps the window
(`SOCIAL_JETSTREAM_WINDOW_SEC`, `SOCIAL_MAX_EVENTS`).

**Reddit is a best-effort feature source:** because keyless JSON is `403`
today, the analyzer marks Reddit in `social.missing` and never blocks the
prediction (a source that is slow or refused is dropped, not awaited).

## CLOB websocket (optional upgrade)

| Endpoint | Method | Auth? | Rate limit | Used for | Status |
|---|---|---|---|---|---|
| `wss://ws-subscriptions-clob.polymarket.com/ws/market` | WS | No (public) | (streaming) | live book/price updates; `--watch` polling is kept as the default, WS is the documented upgrade path (`app/polymarket/ws.py`) | verified |

Subscribe: `{"type": "market", "assets_ids": [...]}`.
Events: `book`, `price_change`, `last_trade_price`.

## Jev — TypeSafe decision model

| Endpoint | Method | Auth? | Rate limit | Used for | Status |
|---|---|---|---|---|---|
| `POST https://api.typesafe.ai/v1/systemone` — body `{"state", "model", "questions"}` | POST | Yes — `Authorization: Bearer JEV_API_KEY` | 1200 req/min, 250k tokens/sec | **one call, three parallel questions**: `news_edge` (noul -> P(true)), `trade_action` (YES/NO/SKIP choice + confidence; option order randomized per call), `signal_strength` (noise/weak/medium/strong -> weighted float) | verified |

Model ID pinned via `JEV_MODEL` (default `jev-1.13.0`) — never `jev-latest`;
the response's `model` field is recorded in the audit log. Pricing: $0.042 /
1M input tokens, output free. Limits: 32k state+longest-question, 64k total.
Latency ~250ms p50. `JEV_MOCK=true` (default) keeps everything offline.

## Story sources

| Endpoint | Method | Auth? | Rate limit | Used for | Status |
|---|---|---|---|---|---|
| Google News RSS (`https://news.google.com/rss/search`) | GET | No | (RSS, polite polling) | story candidates, keyless | verified |
| Outlet RSS (Reuters / AP / BBC / Guardian world feeds) | GET | No | (RSS, polite polling) | story candidates, keyless (best-effort URLs; failures tolerated) | verified |
| GDELT 2.1 DOC API (`https://api.gdeltproject.org/api/v2/doc/doc?query=...&mode=ArtList&format=json&maxrecords=250&timespan=3d`) | GET | No (keyless, free) | (generous) | story candidates (`ArtList`) + volume/tone timelines (`TimelineVol` / `TimelineTone`) for engagement deltas | verified |
| Reddit API (OAuth app-only) | GET | OAuth — `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` / `REDDIT_USER_AGENT` | standard | trending story candidates | verified (flow); **note: Reddit needs pre-approval for new apps since Nov 2025 — optional** |

NewsAPI.org is deliberately NOT used (too expensive). X/Twitter is
**excluded by design** (pay-per-use ~$0.005/read + ToS risk) — no scraper exists.

## Explicitly NOT USED (paper only)

No trading endpoints, no order-placement endpoints, no wallet signing, no
private keys — this is paper trading. Concretely never called:

- `POST /order` (and any order placement / cancellation endpoint) on the CLOB
- Any signing flow (EIP-712 / HMAC / private-key signing) — none exists in code
- Any endpoint that can move real funds, on any API in this inventory

The codebase contains no trading endpoint paths and no signing code; fills are
simulated in `app/execution/paper.py` against public market data (see
`docs/HONESTY.md`).

## Open questions (verify live at build time)

1. **Jev trial-credit / waitlist status on hackathon day** — verify at build
   time; keep gateway/Laya fallbacks warm.
2. **Exact Gamma field names for UMA resolution status** — verify against a
   live `/markets` response (`umaResolutionStatus` candidates in code are
   unverified).
3. **Current direction of favorite-longshot bias on Polymarket** — re-measure
   on 2025–2026 resolved data; don't hardcode a bias correction.
4. **`/public-search` ranking stability** — test empirically before depending
   on top-1 matching.
5. **Per-market fee payload field names** — verify live (`feeRate` candidates
   in code are unverified).
6. **Maker-fill modeling** — out of scope by design (we always cross the spread).
