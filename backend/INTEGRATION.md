# INTEGRATION.md — trading-core ↔ data-layer contract

The trading core (`app/policy`, `app/execution`, `app/memory`, `app/api`,
`app/loop`, `app/playtest`) consumes the sibling-built data layer. This file
pins the exact interfaces used, so both sides can evolve without silent
breakage.

## Config — `app.config.Settings`

Pydantic `BaseSettings`, `extra="ignore"`. Constructed directly in tests with
keyword overrides (e.g. `Settings(EDGE_THRESHOLD=0.05)`). Relevant fields:

| Field | Used by | Notes |
|---|---|---|
| `EDGE_THRESHOLD`, `KELLY_FRACTION`, `MAX_PER_MARKET_FRAC`, `MAX_TOTAL_EXPOSURE_FRAC`, `WIDE_SPREAD_BPS`, `MIN_BOOK_DEPTH_USD`, `MIN_HOURS_TO_RESOLUTION`, `MAX_FEED_STALENESS_SEC` | `RiskGate` | all numeric thresholds |
| `FEE_BPS`, `SLIPPAGE_BPS` | `PaperEngine` | modeled costs |
| `KILL_DRAWDOWN_PCT`, `PAPER_BANKROLL_USD`, `LOOP_INTERVAL_SEC` | `app/loop` | |
| `DATABASE_PATH` | `app/loop`, `app/api` | relative paths resolve under `backend/` |
| `CORS_ORIGINS` | `app/api` | comma-separated string |
| `safe_subset()` | `GET /config` | secrets stripped |

`Settings` has **no** `PARTIAL_FILL_DEPTH_FRAC`; `PaperEngine` reads it via
`getattr(settings, "PARTIAL_FILL_DEPTH_FRAC", 0.25)` so it works with or
without a future config addition.

## Scraper — `app.scraper`

- `Story` dataclass: `title, url, source, engagement_score, published_at, raw_text`.
- `fetch_reddit_trending(settings)`, `fetch_news_trending(settings)`,
  `fetch_gdelt_trending(settings)` → `list[Story]`; each returns `[]` when
  creds/flags are missing (offline-safe). `TwitterSource` is an explicit stub
  — the loop never calls it.
- `filter.dedupe_by_url(stories)`, `filter.filter_by_engagement(stories, min_score=1.0)`.
- `summarizer.summarize(story, max_sentences=3) -> str`.

## Polymarket — `app.polymarket`

- `GammaClient(settings).search_events(query, limit=20) -> list[dict]` (event
  dicts, may nest `markets`), `.get_event(event_id) -> dict`.
  The loop parses market `clobTokenIds`/`outcomes` as JSON-string-or-list and
  takes the YES token; `conditionId`/`id` as market id; `endDate` for
  hours-to-resolution (unknown → 0.0, fail-closed).
- `ClobClient(settings)`: `.get_mid_price(token_id)`, `.get_spread_bps(token_id)`,
  `.get_orderbook(token_id)` → `{"bids": [[price, size]...], "asks": [...]}`,
  `.get_recent_trades(token_id, limit)`. The loop composes the gate snapshot:
  depth = Σ price·size over top-5 bid/ask levels; feed age = age of the newest
  trade timestamp (`inf` when none parseable → `stale_feed` veto).

## Jev — `app.jev.client`

- `DecisionState(news_summary, market_question, yes_price, extra_context="")`.
- `JevClient(settings).decide(state) -> Decision` with `p_true: float`,
  `choice: "YES"|"NO"|"SKIP"`, `mock: bool`. MOCK mode (`JEV_MOCK=true`,
  the default) makes no network calls.
- The loop records `model_choice`/`model_mock` on every signal; a typed
  `SKIP` becomes an explicit `model_skip` veto (the deterministic gate still
  owns the action).

## Shared runtime files (`backend/data/`)

- `state.json` — loop-written portfolio snapshot (positions, recent
  signals/vetoes/fills, bankroll/cash/exposure/equity/drawdown, calibration
  placeholder). Read by the API.
- `killswitch.json` — `{"engaged", "reason", "engaged_at"}`. Written by the
  loop (auto) and `POST /kill|/resume` (manual); read by both.
- `hunchfall.db` — append-only SQLite audit log (`events` table).
