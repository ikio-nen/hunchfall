/**
 * SAMPLE DATA MODE — synthetic demo data, clearly labeled "SAMPLE".
 *
 * Use when the backend is unreachable (or during demos without live APIs).
 * Honesty rule: every object carries `sample: true` and the UI shows a
 * "SAMPLE — synthetic demo data" banner. Never pass this off as real data.
 */

import type {
  CalibrationReport,
  Fill,
  Position,
  Signal,
  Status,
  Veto,
} from "./client";

const sample = { sample: true } as const;

export interface EquityPoint {
  ts: string;
  equity: number;
  sample?: boolean;
}

export const samplePositions: Position[] = [
  { market_id: "0x1a2b", side: "YES", contracts: 120, avg_price: 0.52, current_price: 0.61, unrealized_pnl: 10.8, ...sample },
  { market_id: "0x3c4d", side: "NO", contracts: 85, avg_price: 0.44, current_price: 0.39, unrealized_pnl: 4.25, ...sample },
  { market_id: "0x5e6f", side: "YES", contracts: 200, avg_price: 0.68, current_price: 0.71, unrealized_pnl: 6.0, ...sample },
  { market_id: "0x7a8b", side: "YES", contracts: 60, avg_price: 0.31, current_price: 0.28, unrealized_pnl: -1.8, ...sample },
  { market_id: "0x9c0d", side: "NO", contracts: 150, avg_price: 0.55, current_price: 0.62, unrealized_pnl: -10.5, ...sample },
  { market_id: "0xbeef", side: "YES", contracts: 90, avg_price: 0.47, current_price: 0.52, unrealized_pnl: 4.5, ...sample },
  { market_id: "0xcafe", side: "NO", contracts: 110, avg_price: 0.63, current_price: 0.59, unrealized_pnl: 4.4, ...sample },
  { market_id: "0xdead", side: "YES", contracts: 45, avg_price: 0.74, current_price: 0.73, unrealized_pnl: -0.45, ...sample },
];

export const sampleSignals: Signal[] = [
  { ts: "2026-09-30T00:02:11Z", question: "Will the Fed cut rates in October?", p_true: 0.68, market_price: 0.55, edge: 0.13, decision: "TRADE", ...sample },
  { ts: "2026-09-30T00:09:44Z", question: "Will the Fed cut rates in October?", p_true: 0.71, market_price: 0.62, edge: 0.09, decision: "TRADE", ...sample },
  { ts: "2026-09-30T00:15:02Z", question: "BTC above $150k by year end?", p_true: 0.41, market_price: 0.38, edge: 0.03, decision: "ABSTAIN", ...sample },
  { ts: "2026-09-30T00:21:37Z", question: "SpaceX Starship orbital launch success?", p_true: 0.83, market_price: 0.66, edge: 0.17, decision: "TRADE", ...sample },
  { ts: "2026-09-30T00:28:19Z", question: "AI to win a Nobel Prize this year?", p_true: 0.12, market_price: 0.18, edge: -0.06, decision: "VETOED", ...sample },
  { ts: "2026-09-30T00:34:55Z", question: "UK election called before June?", p_true: 0.55, market_price: 0.51, edge: 0.04, decision: "ABSTAIN", ...sample },
  { ts: "2026-09-30T00:41:08Z", question: "ETH ETF approval this quarter?", p_true: 0.62, market_price: 0.47, edge: 0.15, decision: "TRADE", ...sample },
  { ts: "2026-09-30T00:47:29Z", question: "Mars sample return delayed past 2030?", p_true: 0.77, market_price: 0.71, edge: 0.06, decision: "ABSTAIN", ...sample },
  { ts: "2026-09-30T00:53:51Z", question: "India wins the T20 World Cup?", p_true: 0.34, market_price: 0.26, edge: 0.08, decision: "TRADE", ...sample },
  { ts: "2026-09-30T01:00:03Z", question: "AGI announced by a major lab in 2026?", p_true: 0.09, market_price: 0.14, edge: -0.05, decision: "VETOED", ...sample },
  { ts: "2026-09-30T01:06:26Z", question: "Eurozone enters recession Q1?", p_true: 0.48, market_price: 0.43, edge: 0.05, decision: "ABSTAIN", ...sample },
  { ts: "2026-09-30T01:11:42Z", question: "SpaceX Starship orbital launch success?", p_true: 0.86, market_price: 0.7, edge: 0.16, decision: "TRADE", ...sample },
];

export const sampleVetoes: Veto[] = [
  { ts: "2026-09-30T00:28:19Z", reason: "NEGATIVE_EDGE", detail: "AI Nobel: edge -0.06 below 0.10 threshold", ...sample },
  { ts: "2026-09-30T01:00:03Z", reason: "NEGATIVE_EDGE", detail: "AGI announcement: edge -0.05 below 0.10 threshold", ...sample },
  { ts: "2026-09-29T23:41:07Z", reason: "EXPOSURE_LIMIT", detail: "Position would exceed $500 exposure cap", ...sample },
  { ts: "2026-09-29T22:12:54Z", reason: "LOW_LIQUIDITY", detail: "Spread 12¢ on long-tail market — skipped", ...sample },
  { ts: "2026-09-29T21:03:30Z", reason: "RESOLUTION_RISK", detail: "Ambiguous resolution criteria detected", ...sample },
  { ts: "2026-09-29T20:15:12Z", reason: "NEGATIVE_EDGE", detail: "Edge 0.02 below 0.10 threshold", ...sample },
];

export const sampleFills: Fill[] = [
  { fill_id: "fill-010", side: "BUY", price: 0.55, size_usd: 66.0, fee_usd: 0.066, slippage_usd: 0.31, ts: "2026-09-30T00:02:11Z", ...sample },
  { fill_id: "fill-009", side: "BUY", price: 0.62, size_usd: 52.7, fee_usd: 0.053, slippage_usd: 0.22, ts: "2026-09-30T00:09:44Z", ...sample },
  { fill_id: "fill-008", side: "BUY", price: 0.66, size_usd: 79.2, fee_usd: 0.079, slippage_usd: 0.44, ts: "2026-09-30T00:21:37Z", ...sample },
  { fill_id: "fill-007", side: "BUY", price: 0.47, size_usd: 42.3, fee_usd: 0.042, slippage_usd: 0.19, ts: "2026-09-30T00:41:08Z", ...sample },
  { fill_id: "fill-006", side: "BUY", price: 0.26, size_usd: 23.4, fee_usd: 0.023, slippage_usd: 0.11, ts: "2026-09-30T00:53:51Z", ...sample },
  { fill_id: "fill-005", side: "SELL", price: 0.7, size_usd: 63.0, fee_usd: 0.063, slippage_usd: 0.27, ts: "2026-09-30T01:11:42Z", ...sample },
  { fill_id: "fill-004", side: "BUY", price: 0.58, size_usd: 34.8, fee_usd: 0.035, slippage_usd: 0.18, ts: "2026-09-29T22:40:15Z", ...sample },
  { fill_id: "fill-003", side: "SELL", price: 0.41, size_usd: 28.7, fee_usd: 0.029, slippage_usd: 0.15, ts: "2026-09-29T21:55:02Z", ...sample },
  { fill_id: "fill-002", side: "BUY", price: 0.49, size_usd: 44.1, fee_usd: 0.044, slippage_usd: 0.21, ts: "2026-09-29T20:48:33Z", ...sample },
  { fill_id: "fill-001", side: "SELL", price: 0.33, size_usd: 19.8, fee_usd: 0.02, slippage_usd: 0.09, ts: "2026-09-29T19:30:10Z", ...sample },
];

export const sampleCalibration: CalibrationReport = {
  bins: [
    { bin_mid: 0.1, mean_pred: 0.11, empirical: 0.09, n: 34 },
    { bin_mid: 0.2, mean_pred: 0.21, empirical: 0.24, n: 41 },
    { bin_mid: 0.3, mean_pred: 0.3, empirical: 0.29, n: 38 },
    { bin_mid: 0.4, mean_pred: 0.4, empirical: 0.43, n: 45 },
    { bin_mid: 0.5, mean_pred: 0.5, empirical: 0.47, n: 52 },
    { bin_mid: 0.6, mean_pred: 0.6, empirical: 0.61, n: 47 },
    { bin_mid: 0.7, mean_pred: 0.7, empirical: 0.66, n: 39 },
    { bin_mid: 0.8, mean_pred: 0.8, empirical: 0.83, n: 31 },
    { bin_mid: 0.9, mean_pred: 0.89, empirical: 0.87, n: 22 },
  ],
  accuracy: 0.71,
  abstention_rate: 0.38,
  brier: 0.19,
  ...sample,
};

export const sampleEquityCurve: EquityPoint[] = [
  { ts: "2026-09-29T12:00:00Z", equity: 1000.0, ...sample },
  { ts: "2026-09-29T15:00:00Z", equity: 1004.2, ...sample },
  { ts: "2026-09-29T18:00:00Z", equity: 998.7, ...sample },
  { ts: "2026-09-29T21:00:00Z", equity: 1012.5, ...sample },
  { ts: "2026-09-30T00:00:00Z", equity: 1008.1, ...sample },
  { ts: "2026-09-30T03:00:00Z", equity: 1017.9, ...sample },
  { ts: "2026-09-30T06:00:00Z", equity: 1021.4, ...sample },
  { ts: "2026-09-30T09:00:00Z", equity: 1015.8, ...sample },
];

export const sampleStatus: Status = {
  bankroll: 1000.0,
  equity: 1015.8,
  exposure_usd: 342.6,
  kill_switch_engaged: false,
  mode: "PAPER",
  ...sample,
};
