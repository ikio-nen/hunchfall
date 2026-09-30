/**
 * hunchfall — typed API client (paper trading only).
 *
 * Base URL comes from `VITE_API_BASE` (default http://127.0.0.1:8000).
 * All requests go to the PAPER backend; this UI never places real orders.
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
  ts: string;
  question: string;
  p_true: number;
  market_price: number;
  edge: number;
  decision: string; // e.g. "TRADE" | "ABSTAIN" | "VETOED"
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
  accuracy: number;
  abstention_rate: number;
  brier: number;
  sample?: boolean;
}

export interface Status {
  bankroll: number;
  equity: number;
  exposure_usd: number;
  kill_switch_engaged: boolean;
  mode: string; // "PAPER" expected
  sample?: boolean;
}

export interface Config {
  [key: string]: unknown;
}

// ------------------------------------------------------------ request helpers

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status} ${res.statusText}${text ? `: ${text}` : ""}`);
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

/** Fetch a list endpoint, normalising to an array. */
export async function apiList<T>(path: string): Promise<T[]> {
  const data = await apiGet<T[] | { items: T[] }>(path);
  if (Array.isArray(data)) return data;
  if (data && Array.isArray((data as { items: T[] }).items))
    return (data as { items: T[] }).items;
  return [];
}
