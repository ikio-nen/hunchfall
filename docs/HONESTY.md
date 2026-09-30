# hunchfall — Honesty Rules

These are non-negotiable. The demo, the dashboard, and the pitch all obey them.

## What is never faked

- **Fill prices** come from the live CLOB touch price (taker crosses half the
  spread). If a price is not from the live book, it is labeled **simulated**.
- **Fees use the verified taker formula** `fee = C x feeRate x p x (1-p)`
  (C = filled shares, p = fill price) with category rates — Crypto 0.07,
  Sports/Economics/Culture/Weather/Other 0.05,
  Finance/Politics/Mentions/Tech 0.04, Geopolitics 0.0; makers pay 0 (we
  always take). A per-market payload rate wins when present.
- **Slippage is modeled** as `(simulated fill price − token price) x contracts`
  = half-spread crossing + level-by-level book-walk impact when size exceeds
  1% of visible depth; partial fills when depth < size. `Fill.intended_price`
  (touch) vs `Fill.price` (simulated vwap) are both recorded.
- **P&L** is computed only from recorded paper fills in the audit log. No
  hypothetical "would have made" numbers.
- **No invented win rates.** Every metric shown comes from a playtest round or
  shadow-mode audit log, with the round number and sample count attached.
- **Mock Jev** (`JEV_MOCK=1`) outputs are labeled **MOCK** everywhere they appear.
- **Synthetic samples** are labeled **SAMPLE** (`source` field in the schema);
  real resolved-market samples are labeled **REAL**. The label is never stripped
  in reports or UI.

## How the demo stays honest

- The frontend has a **SAMPLE DATA toggle**. When on, every panel shows a
  `SAMPLE` badge; the audience can see exactly what is illustrative.
- Playtest outputs carry their `source` labels; the 5 built-in `--samples`
  markets are synthetic and say so.
- Losses and vetoes are first-class citizens in the dashboard (see below) —
  a demo that only shows wins is a dishonest demo.

## Veto / loss / fee visibility requirements

The dashboard and every report MUST show:

1. **Veto log** — every veto with its reason (`stale_feed`, `wide_spread`,
   `jev_error`, `near_resolution`, `exposure_cap`, ...), timestamp, and market.
2. **Losses** — losing paper positions listed plainly, not netted away.
3. **Fees & slippage** — the category taker fee (`C x rate x p x (1-p)`) and
   modeled slippage applied to every fill and shown in the P&L breakdown.

If any of these three is missing from a surface, that surface is not shippable.

## The paper-trading-only invariant

This is a structural guarantee, not a promise:

- The codebase contains **no order-placement endpoints** (no trading API calls).
- There is **no wallet signing code** and **no private keys** anywhere —
  none in code, none in env, none in docs.
- The only money-like number in the system is `PAPER_BANKROLL_USD`, a virtual
  float in SQLite.

Any PR that introduces a signing library, a key variable, or a trading endpoint
fails review automatically.
