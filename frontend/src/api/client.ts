/**
 * hunchfall — typed API client (paper trading only).
 *
 * Base URL comes from `VITE_API_BASE` (default http://127.0.0.1:8000).
 * All requests go to the PAPER backend; this UI never places real orders.
 *
 * RFC-001 additions: `apiPostJson`, structured `ApiError` (the backend's
 * `{detail:{code,message,source}}` envelope), and adapters that translate
 * raw backend payloads (audit rows, nested calibration, `*_usd` keys) into
 * the UI types. Nothing here invents numbers.
 */

export const API_BASE: string =
  (import.meta.env.VITE_API_BASE as string | undefined) ??
  "http://127.0.0.1:8000";

/** Thrown when the backend cannot be reached. */
export class BackendUnreachableError extends Error {
  public readonly cause_: unknown;
  constructor(cause?: unknown) {
    super(
      `Backend unreachable at ${API_BASE}. ` +
        "Start the FastAPI server, or enable SAMPLE DATA mode.",
    );
    this.name = "BackendUnreachableError";
    this.cause_ = cause;
  }
}

/** Structured error detail returned by the backend. */
export interface ApiErrorDetail {
  code?: string;
  message?: string;
  source?: string;
  [key: string]: unknown;
}

/** HTTP error carrying the backend's `detail` object. */
export class ApiError extends Error {
  public readonly status: number;
  public readonly detail?: ApiErrorDetail;
  constructor(status: number, statusText: string, body: string) {
    super(`HTTP ${status} ${statusText}${body ? `: ${body}` : ""}`);
    this.name = "ApiError";
    this.status = status;
    let detail: ApiErrorDetail | undefined;
    try {
      const parsed = JSON.parse(body) as { detail?: unknown };
      if (parsed.detail && typeof parsed.detail === "object") {
        detail = parsed.detail as ApiErrorDetail;
      }
    } catch {
      detail = undefined;
    }
    this.detail = detail;
  }
}

// ---------------------------------------------------------------- interfaces

export interface Position {
  market_id: string;
  side: string; // "YES" | "NO"
  contracts: number;
  avg_price: number;
  current_price: number;
  unrealized_pnl: number;
  sample?: boolean;
}

export interface Signal {
  /** Audit row id (enables validation for legacy loop signals). */
  id?: number;
  /** Extension-scan uuid, when present. */
  signal_id?: string;
  ts: string;
  question: string;
  p_true: number;
  market_price: number;
  edge: number;
  decision: string; // "TRADE" | "VETOED" | "YES" | "SKIP" | ...
  side?: string;
  marketplace?: string;
  source?: string;
  model_mock?: boolean;
  model_version?: string;
  /** true/false when the scan gate recorded a decision; null otherwise. */
  approved?: boolean | null;
  sample?: boolean;
}

export interface Veto {
  ts: string;
  reason: string;
  detail: string;
  sample?: boolean;
}

export interface Fill {
  fill_id: string;
  side: string; // "BUY" | "SELL"
  price: number;
  size_usd: number;
  fee_usd: number;
  slippage_usd: number;
  ts: string;
  sample?: boolean;
}

export interface CalibrationBin {
  bin_mid: number;
  mean_pred: number;
  empirical: number;
  n: number;
}

export interface CalibrationReport {
  bins: CalibrationBin[];
  accuracy: number | null;
  abstention_rate: number | null;
  brier: number | null;
  n_validated?: number | null;
  sample?: boolean;
}

/** Honest empty calibration — never mock numbers in live mode. */
export const EMPTY_CALIBRATION: CalibrationReport = {
  bins: [],
  accuracy: null,
  abstention_rate: null,
  brier: null,
  n_validated: 0,
};

export interface Status {
  bankroll: number | null;
  equity: number | null;
  exposure_usd: number | null;
  kill_switch_engaged: boolean;
  mode: string; // "PAPER" expected
  sample?: boolean;
}

export interface EquityPoint {
  ts: string;
  equity: number;
  sample?: boolean;
}

export interface Config {
  [key: string]: unknown;
}

/** Per-marketplace scan/hunch/validation counts (`GET /marketplaces`). */
export interface MarketplaceCount {
  name: string;
  scans: number;
  hunches: number;
  validated: number;
  pending_validations: number;
  last_scan_at?: string | null;
}

/** A registered watch-only wallet. */
export interface WatchWallet {
  address: string;
  label: string;
  watch_only: boolean;
  created_at?: string;
  updated_at?: string;
}

/** One normalized activity row (trade/position) from the wallet view. */
export interface WalletActivityItem {
  source?: string;
  type?: string;
  ts?: string | null;
  market?: string | null;
  side?: string | null;
  outcome?: string | null;
  size_usd?: number | null;
  price?: number | null;
  tx_hash?: string | null;
  size?: number | null;
  avg_price?: number | null;
  current_price?: number | null;
  pnl?: number | null;
}

export interface WalletChain {
  source?: string;
  rpc_url?: string;
  balance_wei?: string;
  balance_pol?: number | null;
  nonce?: number | null;
}

/** `GET /wallets/{address}/activity` response. */
export interface WalletActivity {
  mode?: string;
  wallet: WatchWallet;
  sources: string[];
  degraded: string[];
  activity: WalletActivityItem[];
  positions: WalletActivityItem[];
  chain: WalletChain | null;
  fetched_at?: string;
  note?: string;
  label?: string;
}

// ------------------------------------------------- RFC-003 market guesser types

/** The market block of a prediction response. */
export interface PredictMarket {
  question: string;
  slug: string;
  condition_id: string;
  yes_token_id: string;
  no_token_id: string;
  end_date?: string;
  hours_to_resolution?: number | null;
  volume24hr_usd?: number | null;
  closed?: boolean;
}

/** The seven requested prediction fields live under `prediction`. */
export interface Prediction {
  prediction_id: string;
  p_yes: number;
  direction: "YES" | "NO" | "ABSTAIN";
  confidence: number;
  edge_vs_market: number;
  abstained: boolean;
  reasons: string[];
  snapshot_ts?: string;
}

/** The labelled model block (mock is always explicit). */
export interface PredictModelInfo {
  mode: string; // "mock" | "live"
  mock: boolean;
  version: string;
  ensemble_n: number;
  disagreement: number;
  note?: string;
}

export interface PredictSnapshot {
  sha256: string;
  missing: string[];
  features: Record<string, unknown>;
}

/** `POST /predict` and `GET /predict/demo` response. */
export interface PredictResponse {
  mode: string;
  market: PredictMarket;
  prediction: Prediction;
  market_price: number;
  abstain_threshold: number;
  model: PredictModelInfo;
  snapshot: PredictSnapshot;
  audit_event_id: number | null;
  label: string;
  trade_placed: boolean;
  demo?: boolean;
  snapshot_source?: "live" | "canned";
}

export interface PredictBrier {
  model: number;
  market: number;
  baseline_0_5: number;
  skill_vs_market: number | null;
  skill_vs_baseline: number | null;
  n?: number;
}

export interface PredictBin {
  lo: number;
  hi: number;
  n: number;
  mean_p: number | null;
  event_rate: number | null;
}

/** `GET /predict/accuracy` — derived on read. */
export interface PredictAccuracy {
  as_of: string;
  include_mock: boolean;
  n_logged: number;
  n_resolved: number;
  n_abstained: number;
  n_guessed: number;
  n_resolved_guessed: number;
  abstention_rate: number | null;
  mean_abs_edge: number | null;
  brier: PredictBrier | null;
  brier_guessed: PredictBrier | null;
  direction_accuracy: number | null;
  bins: PredictBin[];
  mock_split: Record<string, { n_logged: number; n_resolved: number }>;
  label: string;
}

/** `POST /predict/{prediction_id}/resolve` response. */
export interface PredictResolution {
  prediction_id: string;
  outcome: "YES" | "NO";
  resolved_at: string;
  prediction: Pick<
    Prediction,
    "p_yes" | "direction" | "confidence" | "abstained"
  >;
  brier: PredictBrier;
  label: string;
}

// ------------------------------------------------------------ request helpers

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, res.statusText, text);
  }
  return (await res.json()) as T;
}

function join(base: string, path: string): string {
  return base.replace(/\/+$/, "") + "/" + path.replace(/^\/+/, "");
}

/** GET JSON from the backend; throws BackendUnreachableError on network failure. */
export async function apiGet<T>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(join(API_BASE, path));
  } catch (cause) {
    throw new BackendUnreachableError(cause);
  }
  return handle<T>(res);
}

/** POST (no body) to the backend; throws BackendUnreachableError on network failure. */
export async function apiPost<T = unknown>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(join(API_BASE, path), { method: "POST" });
  } catch (cause) {
    throw new BackendUnreachableError(cause);
  }
  return handle<T>(res);
}

/** POST a JSON body to the backend; throws ApiError with the backend detail. */
export async function apiPostJson<T = unknown>(
  path: string,
  body: unknown,
): Promise<T> {
  let res: Response;
  try {
    res = await fetch(join(API_BASE, path), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (cause) {
    throw new BackendUnreachableError(cause);
  }
  return handle<T>(res);
}

/** Fetch a list endpoint, normalising to an array. */
export async function apiList<T>(path: string): Promise<T[]> {
  const data = await apiGet<T[] | { items: T[] }>(path);
  if (Array.isArray(data)) return data;
  if (data && Array.isArray((data as { items: T[] }).items))
    return (data as { items: T[] }).items;
  return [];
}

// ------------------------------------------------------------------ adapters

function num(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function numOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

interface RawStatusPayload {
  bankroll_usd?: unknown;
  cash_usd?: unknown;
  exposure_usd?: unknown;
  equity_usd?: unknown;
  drawdown_pct?: unknown;
  kill_switch?: { engaged?: unknown } | null;
  mode?: unknown;
  equity_curve?: Array<{ ts?: unknown; equity_usd?: unknown }> | null;
}

/** Map the backend /status payload to the UI `Status` shape. */
export function toStatus(raw: unknown, fallback: Status): Status {
  const r = (raw ?? {}) as RawStatusPayload;
  return {
    bankroll: numOrNull(r.bankroll_usd) ?? fallback.bankroll,
    equity: numOrNull(r.equity_usd) ?? fallback.equity,
    exposure_usd: numOrNull(r.exposure_usd) ?? fallback.exposure_usd,
    kill_switch_engaged: Boolean(
      r.kill_switch?.engaged ?? fallback.kill_switch_engaged,
    ),
    mode: typeof r.mode === "string" ? r.mode : fallback.mode,
  };
}

/** Equity points from `/status.equity_curve`; [] when absent (never faked). */
export function toEquityPoints(raw: unknown): EquityPoint[] {
  const r = (raw ?? {}) as RawStatusPayload;
  if (!Array.isArray(r.equity_curve)) return [];
  const out: EquityPoint[] = [];
  for (const point of r.equity_curve) {
    if (
      point &&
      typeof point.ts === "string" &&
      typeof point.equity_usd === "number" &&
      Number.isFinite(point.equity_usd)
    ) {
      out.push({ ts: point.ts, equity: point.equity_usd });
    }
  }
  return out;
}

/** Map audit rows (`{id, ts, payload}`) to flat `Signal` rows for the table. */
export function toSignalRows(raw: unknown): Signal[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((entry) => {
    const row = (entry ?? {}) as {
      id?: number;
      ts?: string;
      payload?: Record<string, unknown>;
    };
    const p = (row.payload ?? {}) as Record<string, unknown>;
    const pTrue = numOrNull(p.p_true);
    const price = numOrNull(p.market_price);
    const gate = (p.gate ?? {}) as { approved?: unknown; edge?: unknown };
    const hasGate = typeof gate.approved === "boolean";
    const edge =
      numOrNull(gate.edge) ??
      (pTrue !== null && price !== null ? Math.abs(pTrue - price) : 0);
    const side =
      typeof p.side === "string"
        ? p.side
        : pTrue !== null && price !== null
          ? pTrue > price
            ? "YES"
            : "NO"
          : undefined;
    return {
      id: row.id,
      signal_id: typeof p.signal_id === "string" ? p.signal_id : undefined,
      ts: typeof row.ts === "string" ? row.ts : "",
      question: typeof p.question === "string" ? p.question : "—",
      p_true: pTrue ?? 0,
      market_price: price ?? 0,
      edge,
      decision: hasGate
        ? gate.approved
          ? "TRADE"
          : "VETOED"
        : typeof p.model_choice === "string"
          ? p.model_choice
          : "—",
      side,
      marketplace: typeof p.marketplace === "string" ? p.marketplace : undefined,
      source: typeof p.source === "string" ? p.source : undefined,
      model_mock: typeof p.model_mock === "boolean" ? p.model_mock : undefined,
      model_version:
        typeof p.model_version === "string" ? p.model_version : undefined,
      approved: hasGate ? Boolean(gate.approved) : null,
    };
  });
}

/**
 * Calibration report from `/calibration`.
 *
 * Populated bins only — the backend returns all ten bins with `n=0` when a
 * bucket is empty, and those must not render as fake points at (0, 0).
 */
export function toCalibrationReport(
  raw: unknown,
  fallback: CalibrationReport,
): CalibrationReport {
  const r = (raw ?? {}) as { calibration?: Record<string, unknown> };
  const c = (r.calibration ?? {}) as Record<string, unknown>;
  const rawBins = Array.isArray(c.bins) ? c.bins : [];
  const bins: CalibrationBin[] = [];
  for (const entry of rawBins) {
    const b = (entry ?? {}) as Partial<CalibrationBin>;
    if (typeof b.bin_mid !== "number") continue;
    const n = typeof b.n === "number" ? b.n : 0;
    if (n <= 0) continue;
    bins.push({
      bin_mid: b.bin_mid,
      mean_pred: num(b.mean_pred, 0),
      empirical: num(b.empirical, 0),
      n,
    });
  }
  return {
    bins,
    accuracy: numOrNull(c.accuracy) ?? fallback.accuracy,
    abstention_rate: numOrNull(c.abstention_rate) ?? fallback.abstention_rate,
    brier: numOrNull(c.brier) ?? fallback.brier,
    n_validated: numOrNull(c.n_validated),
  };
}
