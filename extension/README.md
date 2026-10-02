# Hunchfall Extension (MV3)

Page scanner + overlay for prediction marketplaces. You browse a market, the
extension scans the page, the Hunchfall backend returns a **paper** prediction,
and a panel slides in on the page. Read-only: it never clicks, fills, or
submits anything on the marketplace.

## Install (dev)

1. Open `chrome://extensions`, enable **Developer mode**.
2. **Load unpacked** → select this `extension/` folder.
3. Click the Hunchfall icon, set the **Backend URL** (default `http://localhost:8000`).
4. Open any `polymarket.com/event/...` or `/market/...` page — the panel appears bottom-right.

## How it works

```
Polymarket page
  └─ content.js: slug from URL + best-effort visible scrape (prices/title)
        │  { marketplace, kind, slug, url, page_state }
        ▼
service-worker.js ──POST /extension/scan──▶ Hunchfall backend
        │                                        ├─ resolve slug → market (Gamma)
        │                                        ├─ live price/book (CLOB), trades (Data API)
        │                                        └─ Jev typed decision → hunch
        ▼
content.js renders panel: side, P(true), live price, edge, signal id
```

- The scrape is only a **hint** (which market you're looking at). The backend
  re-validates everything through Polymarket's **official** Gamma/CLOB/Data
  APIs — same rule as the rest of the project, no third-party wrappers.
- Every scan carries `marketplace: "polymarket"` so the dashboard can group
  scans, hunches, and validations per marketplace ("Your bets on Polymarket").
  New marketplaces = new `matches` entry + a marketplace string; no redesign.

## Backend contract (for backend owner)

`POST /extension/scan`

Request:
```json
{
  "marketplace": "polymarket",
  "kind": "event",
  "slug": "will-btc-hit-100k-in-2026",
  "url": "https://polymarket.com/event/...",
  "page_state": { "yesPrice": 0.62, "noPrice": 0.38, "title": "..." },
  "scanned_at": "2026-09-30T12:30:00+05:30"
}
```

Response:
```json
{
  "market": { "title": "...", "slug": "...", "condition_id": "..." },
  "hunch": {
    "side": "YES",
    "p_true": 0.68,
    "price": 0.62,
    "edge": 0.06,
    "reason": "order-flow imbalance + ...",
    "signal_id": "uuid"
  }
}
```

Implemented — the backend now serves `POST /extension/scan` (re-validates the
page hint through Gamma, CLOB, and Data API v2, then records a paper
prediction; "paper prediction · no trade placed", no fill is ever placed).
The panel renders the returned hunch, or the backend error state when the
backend is unreachable.

## Honesty & safety

- Panel header always reads **"paper prediction · no trade placed"**.
- The extension runs in the user's own browser on pages they already view; it
  makes no automated requests to the marketplace itself beyond what the page
  loads. All market data comes from Polymarket's public APIs via the backend.
- No wallet, no keys, no order submission — anywhere in this extension.

## Theming

Panel colors live in `panel.css` as `--hf-*` variables. The lime-green concept
is being replaced — when the new palette lands, update the variables here and
mirror them in `frontend/` in one pass.
