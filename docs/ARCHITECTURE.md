# hunchfall — Architecture

Paper-trading-only autonomous Polymarket prediction-market agent.
**No real money. No mainnet orders. No signing. No private keys.** The code cannot
place real orders: there are no trading/signature endpoints and no keys in the codebase.

## The loop (one full cycle)

```
                     +------------------+
                     |     SCRAPER      |  X / Reddit / News / GDELT
                     | (stories, hype)  |  -> story candidates + keywords
                     +--------+---------+
                              |
                     +--------v---------+
                     |      GAMMA       |  stories -> Polymarket events
                     |  (Data API)      |  -> questions, resolution rules,
                     |                  |     end dates, market IDs
                     +--------+---------+
                              |
                     +--------v---------+
                     |       CLOB       |  per market: live YES price
                     |  market data +   |  (GET /price, /midpoint),
                     |  Data API + WS   |  spread (cents), top-of-book
                     |                  |  depth, book levels, recent
                     |                  |  trades, price snapshot ts;
                     |                  |  WS = optional live upgrade
                     +--------+---------+
                              |
                     +--------v---------+
                     |     DATA API     |  /v2/trades (tape, audited),
                     |                  |  /v2/prices-history,
                     |                  |  /v2/activity, /v2/positions
                     |                  |  — read-only market data
                     +--------+---------+
                              |
                     +--------v---------+
                     |  STATE BUILDER   |  joins story + market + book into a
                     |                  |  compact numeric state (NO text
                     |                  |  sent to the decision model)
                     +--------+---------+
                              |
                     +--------v---------+
                     |       JEV        |  TypeSafe hosted API: ONE typed
                     |  (systemone,     |  call (POST /v1/systemone) with
                     |   3 questions)   |  3 parallel questions — noul
                     |                  |  (P(true)), choice (YES/NO/SKIP
                     |                  |  + confidence, option order
                     |                  |  randomized), score (4-level
                     |                  |  legend -> weighted float).
                     |                  |  Model emits NO text. Model ID
                     |                  |  pinned via JEV_MODEL; the
                     |                  |  response's `model` field is
                     |                  |  recorded in the audit log.
                     +--------+---------+
                              |
                     +--------v---------+
                     |   POLICY GATE    |  deterministic, no ML here:
                     |                  |  fee-adjusted edge >= 10%
                     |                  |  (EDGE_THRESHOLD) ?
                     |                  |  spread <= 3c (SPREAD_VETO_CENTS) ?
                     |                  |  top-book depth >= $50 ?
                     |                  |  news/price skew <= 15 min ?
                     |                  |  negrisk sum in [0.98, 1.02] ?
                     |                  |  no UMA dispute ?
                     |                  |  jev confidence >= 0.6 ?
                     |                  |  hours-to-resolution >= 2h ?
                     |                  |  exposure caps + max 5 positions ?
                     |                  |  quarter-Kelly sizing
                     |                  |  -> TRADE / VETO (logged w/ reason)
                     +--------+---------+
                              |
                     +--------v---------+
                     | PAPER EXECUTION  |  virtual taker fill vs live CLOB
                     |                  |  touch: half-spread cross +
                     |                  |  book-walk impact when size > 1%
                     |                  |  of visible depth; partial fills
                     |                  |  when depth < size; taker fee
                     |                  |  fee = C x rate x p x (1-p) by
                     |                  |  category; fills recorded to
                     |                  |  audit log (intended vs simulated
                     |                  |  price both recorded)
                     +--------+---------+
                              |
                     +--------v---------+
                     |    AUDIT LOG     |  append-only SQLite (DATABASE_PATH):
                     |  (+ kill switch |  fills, vetoes, prices, Jev outputs,
                     |     checks)      |  drawdown, kill events
                     +--------+---------+
                              |
              +------------+  |  +----------------------+
              | FastAPI    |<-+--| React dashboard      |
              | (app.api)  |  |  | (reads audit + state |
              |            |  +->|  via FastAPI)         |
              +------------+     +----------------------+
```

The **kill switch** lives in two places:

1. **Loop checks** — before every cycle the loop reads the audit log: if portfolio
   drawdown from peak >= `KILL_DRAWDOWN_PCT`, the loop halts trading and records
   a `KILL_AUTO` event. No cycle can trade without passing this check.
2. **Manual kill** — `POST /kill` on the FastAPI server sets a persisted kill flag.
   The loop checks it at the top of every cycle; dashboard shows a red KILLED banner.
   Re-arming requires an explicit `POST /rearm` (and is itself audit-logged).

## Module responsibilities

| Module | Path | Responsibility |
|---|---|---|
| Scraper | `app/scraper/` | Poll Reddit (optional, needs pre-approval) / RSS news (Google News + outlets, keyless) / GDELT for trending stories; emit `(story_text, keywords, source, ts)`. X excluded by design. No decisions, no prices. |
| Gamma client | `app/polymarket/gamma.py` | Verified endpoints: `/events`, `/markets`, `/public-search` (primary story->market matcher), slugs, tags/series/sports. Keyset pagination (`next_cursor` -> `after_cursor`). Parses JSON-string `outcomePrices` + comma-separated `clobTokenIds`; guards the `["0","0"]` resolved-market gotcha. |
| CLOB client | `app/polymarket/clob.py` | Verified market-data endpoints: `/price`, `/midpoint`, `/book` (min_order_size, tick_size, last_trade_price), `/spread`, `/prices-history`, `/tick-size`, batch `/books` `/prices` `/midpoints`. **No trading endpoints — `POST /order` is never called.** |
| Data API client | `app/polymarket/data_api.py` | `/v2/trades` (tape, `condition=`), `/v2/prices-history`, `/v2/activity`, `/v2/positions` — read-only market data. **v1 (`/trades`, `/holders`, `/oi`) retired 2026-10-24 and was removed.** |
| WebSocket feed | `app/polymarket/ws.py` | Optional upgrade: `wss://ws-subscriptions-clob.polymarket.com/ws/market`, subscribe `{"type":"market","assets_ids":[...]}`; events `book` / `price_change` / `last_trade_price`. `--watch` keeps REST polling by default. |
| Jev client | `app/jev/client.py` | ONE typed call (`POST /v1/systemone`, Bearer auth, model pinned via `JEV_MODEL`): noul -> P(true), choice (YES/NO/SKIP + confidence, option order randomized per call), score (4-level legend -> weighted float). Text output is impossible by construction. `JEV_MOCK=true` for offline runs (outputs labeled MOCK). |
| Policy gate | `app/policy/gate.py` | Pure deterministic rules: fee-adjusted edge threshold, quarter-Kelly sizing (capped 10%/market, 40% total, max 5 positions), spread/depth/skew/negrisk/UMA/confidence filters. Outputs TRADE (size) or VETO (reason). Every veto is logged. |
| Paper execution | `app/execution/paper.py` | Virtual taker fills at live CLOB touch (half-spread cross) + level-by-level book-walk when size > 1% of visible depth; partial fills when depth < size; taker fee `fee = C x rate x p x (1-p)` by category. Records intended vs simulated price. Never touches a trading endpoint. |
| Audit log | `app/memory/audit.py` | Append-only storage (SQLite). Fills, vetoes, Jev outputs (incl. model version), prices, drawdown, kill events. The dashboard and playtest read this. |
| API | `app/api/` | FastAPI server: `GET /state`, `GET /positions`, `GET /vetoes`, `GET /pnl`, `POST /kill`, `POST /rearm`, `GET /health`, `GET /config` (secrets stripped). Reads audit + latest state. |
| Loop | `app/loop.py` | Orchestrates one full cycle (`--once`) or continuous (`--watch`, `LOOP_INTERVAL_SEC`). Performs kill-switch checks (auto at -15% paper drawdown). |
| Playtest | `app/playtest/` | Labeled resolved-market harness: 60/20/20 time-based split, accuracy, abstention, Brier, calibration (temperature/Platt scaling on the calibration split). See `docs/PLAYTESTING.md`. |

## Data flow of one full cycle

1. **Scraper** emits story candidates from Reddit / RSS news / GDELT (X excluded).
2. **Gamma client** maps each story -> matching markets via `/public-search`
   (event search as fallback), with resolution rules and end dates.
3. **CLOB + Data API clients** pull live book state per market: YES mid price,
   spread in cents, top-of-book USD depth, book levels, trade tape, price
   snapshot timestamp.
4. **Loop** builds the compact state text (news summary + question + YES
   price) — the only thing Jev sees.
5. **Jev** returns, in one call: noul P(true), typed choice YES/NO/SKIP with
   confidence, and a 4-level signal-strength score. No text anywhere. The
   response's `model` field is recorded in the audit log.
6. **Policy gate** checks the *fee-adjusted* edge `|P(true) - price| - fee`
   against `EDGE_THRESHOLD` (10%) plus all filters (3c spread, $50 depth,
   15-min news/price skew, negrisk band, UMA dispute, 0.6 confidence,
   2h-to-resolution); sizes with quarter-Kelly bounded by 10%/market,
   40% total, max 5 positions. Vetoes carry a reason.
7. **Paper execution** records a virtual taker fill (half-spread cross +
   book-walk, category taker fee, partial fills) — or nothing on veto.
8. **Audit log** appends everything; dashboard reads it live.

## Failure modes

| Failure | Detection | Behavior |
|---|---|---|
| News stale vs price (news older than price snapshot by > 15 min, or timestamp unknown) | news_ts / price_ts skew check in the gate | **Veto**: `news_price_skew` / `news_price_skew_unknown`. No trade on stale news. |
| Wide spread (`spread_cents/100 > SPREAD_VETO_CENTS`) | policy gate | **Veto**: `wide_spread`. Illiquid market, skip. |
| Thin top-of-book (`top_book_depth_usd < $50`) | policy gate | **Veto**: `thin_book`. |
| Broken book (`negrisk_sum` outside [0.98, 1.02], incl. `["0","0"]` with no confirmed winner) | policy gate | **Veto**: `negrisk_sum_invalid`. |
| UMA dispute | `uma_dispute` flag | **Veto**: `uma_dispute`. |
| Jev unreachable / error | Jev client timeout/exception | **Abstain**: cycle records `jev_error`, no position. |
| Jev abstains (choice SKIP) or low confidence (< 0.6) | gate | **Veto**: `model_skip` / `low_jev_confidence`. |
| Too many positions (>= 5) | gate | **Veto**: `max_positions`. |
| Drawdown >= 15% | loop pre-cycle check | **Auto kill**: halt trading, kill event, dashboard banner. |
| Market near resolution (`hours < 2`) | policy gate | **Veto**: `near_resolution`. |
| Exposure cap hit | policy gate | **Veto**: `exposure_cap`. |
| Manual kill | `POST /kill` flag | Loop stops trading at next check; positions stay paper-only. |

Every failure path defaults to **do nothing** (veto/abstain), never to trading.
