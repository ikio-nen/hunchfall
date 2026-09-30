# hunchfall — Frontend (Paper Trading UI)

React 18 + Vite + TypeScript dashboard for hunchfall, the autonomous Polymarket
**paper-trading** agent (team "dead Wallets", Hefty Hacks). Dark trading-desk theme.

## Locked rules this UI enforces

- **PAPER TRADING ONLY.** Every screen is visibly labeled PAPER; there are no
  real-money affordances anywhere.
- **Honesty first.** The Honesty panel (fees, slippage, losing fills, vetoes) is
  pinned at the bottom of every screen. Numbers come only from the backend or
  SAMPLE data — never invented. "Losing fills" uses a stated, transparent rule:
  a fill counts as losing when fee + slippage > 1% of its size (realized PnL
  isn't available from fills alone). This rule is shown in a tooltip on the cell.

## Install & run

```bash
npm install
npm run dev        # http://127.0.0.1:5173
npm run build      # type-checks (tsc) + vite build
npm run preview
```

## Backend API contract

The FastAPI backend lives at `VITE_API_BASE` (default `http://127.0.0.1:8000`;
see `.env.example`).

| Method | Path          | Returns                                                        |
| ------ | ------------- | -------------------------------------------------------------- |
| GET    | `/positions`  | list of `{market_id, side, contracts, avg_price, current_price, unrealized_pnl}` |
| GET    | `/signals`    | list of `{ts, question, p_true, market_price, edge, decision}` |
| GET    | `/vetoes`     | list of `{ts, reason, detail}`                                 |
| GET    | `/fills`      | list of `{fill_id, side, price, size_usd, fee_usd, slippage_usd, ts}` |
| GET    | `/calibration`| `{bins:[{bin_mid, mean_pred, empirical, n}], accuracy, abstention_rate, brier}` |
| GET    | `/status`     | `{bankroll, equity, exposure_usd, kill_switch_engaged, mode}`  |
| POST   | `/kill`       | engages kill switch                                            |
| POST   | `/resume`     | disengages kill switch                                         |
| GET    | `/config`     | agent config (any shape)                                       |

List endpoints may return either a bare array or `{items: [...]}` — the client
normalises both. `/equity` (time-series) is tried opportunistically on the
Overview page; if the backend doesn't expose it, the page falls back to showing
current equity instead of inventing history.

## SAMPLE DATA mode

- The header has a **SAMPLE DATA** switch. When ON, every page swaps the live
  backend for synthetic data in `src/api/sampleData.ts`.
- A banner appears: **"SAMPLE — synthetic demo data. Not real market data, not
  real trades."** Every sample object carries `sample: true`.
- When the backend is unreachable and SAMPLE mode is off, pages show a
  "backend unreachable" banner instead of zeros or fake numbers.
- The kill switch in SAMPLE mode toggles local state only and says so
  explicitly (no fake POST to a backend that isn't there).

## Layout

```
src/
  api/client.ts       typed fetch client + interfaces
  api/sampleData.ts   synthetic demo dataset (all sample: true)
  state/dataMode.tsx  sample-mode context + fetch helpers
  components/         Nav, StatCard, EquityChart, ReliabilityDiagram,
                      DataTable, KillButton, HonestyPanel
  pages/              Overview, Positions, Signals, Vetoes, Fills, Calibration
  App.tsx             tabs + header (PAPER badge, SAMPLE toggle) + honesty bar
```
