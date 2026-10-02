# muse-scans — Muse's market trend feed for the Hunchfall backend

Every ~6 hours a scheduled job scans Polymarket's public API for the
highest-volume markets and writes the result here
(`backend/data/muse-scans/latest.json`).

## Files

- `latest.json` — the most recent scan (overwrite each run)
- `history.jsonl` — one JSON object per scan, appended (full history)

## Schema (matches `DecisionState` in `backend/app/jev/client.py`)

```json
{
  "scanned_at": "2026-09-30T10:00:00+00:00",
  "source": "muse-scan",
  "markets": [
    {
      "market_question": "Will BTC hit $100k in 2026?",
      "slug": "will-btc-hit-100k-in-2026",
      "yes_price": 0.62,
      "volume_24h": 1234567.0,
      "news_summary": "2-3 sentences on why this market is moving.",
      "extra_context": "volume +40% vs 7d avg; YES up 8pts in 24h; resolves YES if ..."
    }
  ]
}
```

## How the backend consumes it (Aska)

Read `latest.json` each loop cycle and treat each entry as a candidate
`DecisionState`:

- `news_summary` → `DecisionState.news_summary`
- `market_question` → `DecisionState.market_question`
- `yes_price` → `DecisionState.yes_price`
- `extra_context` → `DecisionState.extra_context`

Then run the normal Jev call + risk gate + paper engine. The scan is a
hint, not a decision — the gate still owns execution policy. If the file
is missing or stale (>24h), ignore it and run the normal loop.
