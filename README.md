# hunchfall

![PAPER TRADING ONLY](https://img.shields.io/badge/paper--trading--only-no%20real%20money-blue)
![NOT FINANCIAL ADVICE](https://img.shields.io/badge/not--financial--advice-education%20%26%20demo-lightgrey)

**An autonomous agent that paper-trades Polymarket prediction markets — it reads the news, prices the odds with a typed AI decision layer, and only bets when the math clears a 10% edge. Paper money only. Every decision is auditable.**

## The concept

hunchfall is a **paper-trading** agent for Polymarket prediction markets. It
watches trending stories, matches them to Polymarket markets, reads live order
books, gets a typed probability from the Jev decision API, and only places a
*virtual* bet when a deterministic risk gate says the edge is real. Every
veto, fill, and fee lands in an append-only audit log, and a dashboard shows
it all — including the losses.

Built by team **dead Wallets** (4 people) for the Hefty Hacks
**Finance x Trading** track (Oct 31 – Nov 1, 2026).

**Hard invariants:** paper trading ONLY — no real money, no order-placement
endpoints, no wallet signing code, no private keys anywhere in code, env, or
docs.

## How it works — the loop

1. **SCRAPE** — trending stories from GDELT 2.1 DOC API (free, keyless), news
   RSS, optional Reddit. X/Twitter excluded by design (paywalled API).
2. **MATCH** — Polymarket Gamma Data API `/public-search` maps story → market:
   question, outcomes, resolution rules, UMA oracle info.
3. **PRICE** — CLOB + Data APIs (public, no auth): live YES price, spread in
   cents, top-book depth, book levels, trade tape.
4. **DECIDE** — Jev (TypeSafe hosted API, `POST /v1/systemone`, model
   `jev-1.13.0` pinned): ONE typed call, three parallel questions — noul
   P(true), YES/NO/SKIP choice + confidence, 4-level signal strength.
   **The model emits zero text.** Mock mode (`JEV_MOCK=1`) outputs labeled MOCK.
5. **GATE** — deterministic policy code (no AI here): fee-adjusted edge ≥
   **10%**; quarter-Kelly sizing; vetoes — spread > 3¢, depth < $50,
   < 2h to resolution, news-price skew > 15 min, NegRisk price-sum outside
   0.98–1.02, UMA-disputed market, Jev confidence < 0.6, insufficient post-fee
   edge, SKIP output. Caps: 10% bankroll per market, 40% total exposure, max
   5 positions. Every veto logged with a reason.
6. **RECORD** — paper fills at live CLOB touch (+ half-spread cross, book-walk
   impact, taker fee `C × rate × p × (1−p)` by category). Partial fills when
   depth < size. Append-only SQLite audit log.
7. **SHOW** — React dashboard: overview + paper equity curve, positions,
   signals (P(true) vs price vs edge), veto log, fill ledger, calibration
   chart, honesty panel, kill switch. Kill: `POST /kill` manually, or
   automatically at **−15%** paper drawdown.

Key numbers: edge ≥ 10% · quarter-Kelly · $10,000 virtual bankroll ·
10% per-market / 40% total caps · max 5 positions · −15% auto-kill ·
Jev decision call ≈ $0.00004 · Polymarket APIs free, no auth.

## Problems it solves

1. **Bots lie with vanity metrics** — public leaderboards inflate win rates ~2×
   (~25% wash volume, ~16% farmers). We show only settled, fee-visible, audited
   track records.
2. **LLM agents hallucinate prose into orders** — free-text "theses" become
   trades. Jev emits typed numbers only; no LLM text anywhere in the loop.
3. **Backtests lie** — mid-price backtests ignore spread, fees, slippage. Our
   fills model half-spread crossing, book-walk impact, real taker fees; reports
   are hash-verifiable.
4. **Black-box risk** — most bots can't say why they passed. Every veto is
   logged with a reason; calibration panel; kill switch.
5. **Real-money danger in demos** — paper trading only, by construction (no
   order endpoints, no signing, no keys).
6. **Stale-data traps** — news older than the price, thin books, near-resolution
   markets. Skew / depth / time-to-resolution vetoes.
7. **UMA settlement risk** — disputed oracle resolutions (March 2025: $7M
   Ukraine market falsely resolved, no refunds). Disputed markets are vetoed
   by design.

## Quickstart

### Backend

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env          # default JEV_MOCK=true = fully offline
python -m app.loop --once     # run one full cycle
uvicorn app.api.run:app        # start the API (dashboard backend)
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Point it at the API with `VITE_API_BASE` (default `http://localhost:8000`).

### Playtest (validation before any claim)

```bash
python -m app.playtest --samples                                   # 5 synthetic SAMPLE markets (smoke test)
python -m app.playtest --file data/labeled.jsonl --split tuning    # tune thresholds (never on test)
python -m app.playtest --file data/labeled.jsonl --split test      # official round vs naive baseline
```

Full protocol — 60/20/20 time-based split, accuracy, abstention rate, Brier
score, calibration, reliability-diagram reading, shadow-mode graduation
criteria: `docs/PLAYTESTING.md`.

### Shadow mode (live data, still paper)

```bash
python -m app.loop --watch      # continuous loop, LOOP_INTERVAL_SEC between cycles
```

Shadow mode = live data, **paper fills only**, kill switch armed. Graduate to
it only after the playtesting criteria in `docs/PLAYTESTING.md` pass.
`POST /kill` halts the loop manually; drawdown ≥ `KILL_DRAWDOWN_PCT`
auto-kills; `POST /resume` re-arms it.

### Live Jev

Set `JEV_API_KEY` and `JEV_MOCK=false`. Never commit keys.

## Environment variables

| Name | Default | Purpose |
|---|---|---|
| `POLYMARKET_GAMMA_URL` | `https://gamma-api.polymarket.com` | Gamma Data API base URL |
| `POLYMARKET_CLOB_URL` | `https://clob.polymarket.com` | CLOB market-data base URL (no trading endpoints) |
| `POLYMARKET_WS_URL` | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | CLOB websocket (optional `--watch` upgrade) |
| `POLYMARKET_DATA_API_URL` | `https://data-api.polymarket.com` | Data API **v2** base URL (trades/history/activity/positions; v1 retired) |
| `GDELT_DOC_URL` | `https://api.gdeltproject.org/api/v2/doc/doc` | GDELT 2.1 DOC API base (keyless) |
| `JEV_API_URL` | `https://api.typesafe.ai` | TypeSafe Jev base (live call is `POST /v1/systemone`) |
| `JEV_API_KEY` | — | Jev API key (secret) |
| `JEV_MODEL` | `jev-1.13.0` | Pinned model version — never `jev-latest` |
| `JEV_MOCK` | `true` | `true` = offline mock Jev, outputs labeled MOCK |
| `EDGE_THRESHOLD` | `0.10` | min fee-adjusted \|P(true) − market price\| to trade (10%) |
| `KELLY_FRACTION` | `0.25` | fractional Kelly multiplier (quarter-Kelly) |
| `MAX_PER_MARKET_FRAC` | `0.10` | max bankroll fraction per market |
| `MAX_TOTAL_EXPOSURE_FRAC` | `0.40` | max total open exposure fraction |
| `MAX_CONCURRENT_POSITIONS` | `5` | max open positions (gate vetoes beyond) |
| `SPREAD_VETO_CENTS` | `0.03` | veto if spread exceeds this (USD; 0.03 = 3 cents) |
| `MIN_TOP_DEPTH_USD` | `50.0` | veto if top-of-book depth below this (USD) |
| `MIN_HOURS_TO_RESOLUTION` | `2.0` | veto if market resolves sooner than this |
| `MAX_NEWS_PRICE_SKEW_SEC` | `900` | veto if news older than price snapshot by more than this |
| `NEGRISK_SUM_MIN` / `NEGRISK_SUM_MAX` | `0.98` / `1.02` | negrisk price-sum sanity band |
| `JEV_CONFIDENCE_MIN` | `0.6` | veto if Jev choice confidence below this |
| `KILL_DRAWDOWN_PCT` | `15` | auto kill-switch drawdown threshold (%) |
| `PAPER_BANKROLL_USD` | `10000` | virtual starting bankroll (paper only) |
| `FEE_RATE_OVERRIDE` | `0.0` | per-market authoritative fee rate override (0 = category table) |
| `POLYGON_RPC_URL` | `https://polygon-rpc.com` | public Polygon RPC (read-only balance/nonce only) |
| `WALLET_ACTIVITY_LIMIT` | `50` | items fetched per wallet-activity source |
| `WALLET_ACTIVITY_TTL_SEC` | `60` | in-process wallet-activity cache TTL (0 = off) |
| `WALLET_LABEL_MAX_CHARS` | `64` | watch-only wallet label length cap |
| `SCAN_CLOCK_SKEW_SEC` | `300` | reject extension scans with client clock drift beyond this |
| `SCAN_PAGE_STATE_MAX_BYTES` | `4096` | cap on untrusted page_state (hashed, never stored raw) |
| `PREDICT_ABSTAIN_EDGE` | `0.10` | guesser abstains when \|P(YES) − market\| is below this |
| `PREDICT_ENSEMBLE_N` | `3` | model runs per prediction (median wins), clamped 1–5 |
| `PREDICT_TAPE_LIMIT` | `100` | `/v2/trades` page size for the predictor tape |
| `PREDICT_BOOK_LEVELS` | `5` | book depth levels per side |
| `PREDICT_DEMO_SLUG` | `will-bitcoin-hit-100k-in-2026` | pinned demo market |
| `PREDICT_DEMO_LIVE` | `true` | `false` = demo always serves the canned snapshot |
| `SOCIAL_ENABLED` | `true` | social pulse for social-outcome markets (a feature, never the decision) |
| `SOCIAL_DEADLINE_SEC` | `4.0` | one shared deadline across social sources |
| `SOCIAL_JETSTREAM_URL` | `wss://jetstream2.us-east.bsky.network/subscribe` | keyless Bluesky Jetstream (live-verified) |
| `SOCIAL_JETSTREAM_WINDOW_SEC` | `3.0` | bounded connect → count → close window |
| `SOCIAL_MAX_EVENTS` | `2000` | hard event cap per Jetstream window |
| `SOCIAL_REDDIT_ENABLED` | `true` | keyless Reddit JSON (best-effort; 403s degrade to `missing`) |
| `SOCIAL_RSS_MAX_ITEMS` | `10` | RSS items per social pulse |
| `REDDIT_CLIENT_ID` | — | Reddit API client id (optional; needs pre-approval) |
| `REDDIT_CLIENT_SECRET` | — | Reddit API client secret |
| `REDDIT_USER_AGENT` | `hunchfall` | Reddit API user agent |
| `GDELT_ENABLED` | `1` | `1` = use GDELT 2.1 DOC API in scraper |
| `TWITTER_ENABLED` | `0` | always `0` — X excluded by design |
| `LOOP_INTERVAL_SEC` | `300` | seconds between `--watch` cycles |
| `DATABASE_PATH` | `data/hunchfall.db` | SQLite audit-log path |
| `API_HOST` | `127.0.0.1` | FastAPI bind host |
| `API_PORT` | `8000` | FastAPI bind port |
| `CORS_ORIGINS` | `http://localhost:5173` | allowed CORS origins |
| `VITE_API_BASE` | `http://localhost:8000` | frontend -> API base URL |

## API

`GET /status` · `GET /positions` · `GET /signals` · `GET /vetoes` ·
`GET /fills` · `GET /calibration` · `POST /kill` · `POST /resume` ·
`GET /config` (safe subset).

RFC-003 market guesser (prediction only — no gate, no sizing, no fills):
`POST /predict` · `GET /predict/demo` · `GET /predict/accuracy` ·
`POST /predict/{prediction_id}/resolve`. Every response carries
**“paper prediction · no trade placed”**; the mock path is labelled
`MOCK — not a real model`. Full endpoint notes: `docs/API_INVENTORY.md`.

## Demo script (~3 minutes)

1. **Live veto** — feed a wide-spread market; the gate rejects it with a logged reason.
2. **Kill the phantom arb** — NegRisk mis-sum market; the gate rejects it publicly.
3. **Edge in action** — P(true) 0.72 vs market price 0.58 → 14% post-fee edge → paper fill, fee shown.
4. **Kill switch** — `POST /kill` mid-run; loop halts; dashboard shows halted state.
5. **Honesty panel** — calibration chart next to a vanity leaderboard; veto log and losses visible.

## Honesty rules

PAPER TRADING SIMULATION. Not financial advice.

- Fill prices come from the live CLOB touch or are labeled **simulated**.
- Fees use the verified taker formula `fee = C × rate × p × (1−p)` by category.
- P&L only from recorded paper fills. No invented win rates — every metric
  carries its playtest round number and sample count.
- Mock outputs labeled **MOCK**; synthetic labeled **SAMPLE**; real resolved
  labeled **REAL**. Labels are never stripped.
- Vetoes, losses, and fees are always visible. A surface missing any of the
  three is not shippable.
- "PAPER TRADING SIMULATION. Not financial advice." on every public surface.

Full policy: `docs/HONESTY.md`.

## Roadmap — what's next

1. **Verified track-record leaderboard** — settled-only, fee-visible calibration
   leaderboard (existing leaderboards inflate win rates ~2× with wash volume).
2. **Phantom-arb rejection scanner** — publicly rejects fake NegRisk arbs that fool other bots.
3. **Copy-signal validator** — simulates realistic fills, fees, and latency before
   you copy anyone (naive copy-trading underperforms 60–80%).
4. **Hash-verified backtest reports** — shareable links anyone can verify.
5. **Paper-trading competitions** — users compete on the platform, free to enter.

## Growth notes

- **No paid ads pre-demo.** Google crypto ads are a dead end for an Indian team
  (certification + license required, India not approved); Meta needs prior
  permission; X only after a live demo with a working funnel. Grow free:
  X organic, GitHub, prediction-market Discords.
- **No token.** Cheap to launch, brutal to own — India's 30% flat crypto tax +
  1% TDS and new reporting rules, EU whitepaper requirements, US securities-law
  exposure — and a token would detonate the honesty-first credibility with judges.
- **Later, if ever:** freemium → paid API → sponsorships. Traction first,
  monetization second.

## Docs

- `docs/ARCHITECTURE.md` — the loop, module map, data flow, failure modes
- `docs/API_INVENTORY.md` — verified endpoint paths, rate limits, open questions
- `docs/PLAYTESTING.md` — validation protocol: splits, metrics, calibration, graduation criteria
- `docs/HONESTY.md` — what is never faked, label rules, audit guarantees

## Project layout

```text
hunchfall/
├── README.md
├── docs/                     # ARCHITECTURE, API_INVENTORY, PLAYTESTING, HONESTY
├── backend/
│   ├── app/
│   │   ├── api/              # FastAPI routes (status, positions, signals, vetoes, fills, calibration, kill, resume, config)
│   │   ├── config.py         # env parsing + defaults
│   │   ├── execution/        # paper fill simulation (touch, half-spread, book-walk, fees)
│   │   ├── jev/              # TypeSafe Jev client (typed noul/choice/score) + mock mode
│   │   ├── loop.py           # orchestration loop (--once / --watch)
│   │   ├── memory/           # append-only SQLite audit log
│   │   ├── playtest/         # labeled-set harness, rounds, naive baseline, calibration
│   │   ├── policy/           # deterministic gate: edge, Kelly, caps, vetoes
│   │   ├── polymarket/       # Gamma, CLOB (REST + WS), Data API clients
│   │   └── scraper/          # GDELT / RSS / Reddit (+ X-exclusion stub)
│   ├── requirements.txt
│   └── .env.example
└── frontend/                 # React 18 + Vite + TypeScript dashboard
    └── src/
        ├── pages/            # overview, positions, signals, vetoes, fills, calibration
        ├── components/       # equity curve, honesty panel, kill-switch control
        ├── api/              # typed API client
        └── state/            # dashboard state
```

## Team

**dead Wallets** — Aska (backend) · Saswata "Ikio" Howladar (backend + pitch) ·
Tapabroto "Tapo" Chandra Pal (frontend) · Sumita (web3 → role TBD after the v2 pivot).

## License

PAPER TRADING SIMULATION. Not financial advice.

hunchfall is a hackathon demo and research prototype. Paper simulation on
public market data — no orders were placed, no wallet was connected, and
nothing here is a recommendation to trade. No license file is included yet;
the code belongs to team dead Wallets.
