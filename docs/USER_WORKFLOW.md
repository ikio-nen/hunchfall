# User workflow

![User workflow diagram](user-workflow.png)

How a user moves through the hunchfall dashboard, and how the AI proves its
predictions against live Polymarket prices without ever placing a trade.

## The six screens

| # | Screen | What the user sees | API |
|---|--------|-------------------|-----|
| 1 | Overview | Paper bankroll equity curve, open paper positions, today's hunches, kill-switch state | `GET /status` |
| 2 | Hunches | Prediction feed — market, P(true), live price, edge %, YES/NO side, timestamp; tap for full reasoning | `GET /signals` |
| 3 | Validation ⭐ | Did the AI get it right? Predicted direction vs live price now, HIT ✓ / MISS ✗ stamped per hunch | `POST /signals/{id}/validate` (built — RFC-001) |
| 4 | Positions | Paper book — simulated fills at live CLOB prices, fees applied, P&L, MOCK labels | `GET /positions` |
| 5 | Vetoes | Every abstention with its reason (spread too wide, UMA risk, low confidence…) | `GET /vetoes` |
| 6 | Kill switch | Big red button — halts the loop, flattens the paper book; resume is human-only | `POST /kill` |

User path: **1 → 2 → 3** is the demo; **4–5** are the books; **6** is the
ever-present escape hatch.

## The demo loop (screen 3)

1. AI emits a hunch → backend snapshots the **live** Polymarket price (t0).
2. Wait 1h / 6h / 24h — or the judge taps "Check now".
3. Re-fetch the live price for the same market (t1) from the CLOB. No trade.
4. Direction matched? Stamp **HIT ✓ / MISS ✗** on the hunch and update the
   scoreboard + calibration page.

No order placed. No wallet touched. The live market itself is the scoreboard.

## Implemented in RFC-001

- `POST /signals/{id}/validate` — records the HIT/MISS verdict (append-only),
  re-checks the live CLOB price as evidence, and updates calibration on read.
- `POST /extension/scan` — re-validates a market-page hint through Gamma,
  CLOB, and Data API v2 before recording a paper prediction
  (“paper prediction · no trade placed” — no fill is ever placed).
- `POST /wallets` + `GET /wallets/{address}/activity` — watch-only registry
  (secrets rejected with 400) and read-only activity with graceful degradation.
- `GET /marketplaces` — per-marketplace scans/hunches/validations.
