import { useState } from "react";
import type { FormEvent } from "react";
import type { WalletActivity, WalletActivityItem, WatchWallet } from "../api/client";
import { ApiError, apiGet, apiPostJson } from "../api/client";
import DataTable, { type Column } from "../components/DataTable";
import { useData } from "../state/dataMode";

function fmtTs(ts?: string | null): string {
  if (!ts) return "—";
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts : d.toLocaleString([], { hour12: false });
}

const money = (n?: number | null) =>
  typeof n === "number" && Number.isFinite(n) ? `$${n.toFixed(2)}` : "—";
const price = (n?: number | null) =>
  typeof n === "number" && Number.isFinite(n) ? n.toFixed(3) : "—";

const activityColumns: Column<WalletActivityItem>[] = [
  { key: "ts", header: "Time", render: (r) => fmtTs(r.ts) },
  { key: "type", header: "Type", render: (r) => r.type ?? "—" },
  {
    key: "market",
    header: "Market",
    render: (r) => <span className="q">{r.market ?? "—"}</span>,
  },
  { key: "side", header: "Side", render: (r) => r.side ?? "—" },
  { key: "size", header: "Size", render: (r) => money(r.size_usd), className: "num" },
  { key: "price", header: "Price", render: (r) => price(r.price), className: "num" },
  {
    key: "tx",
    header: "Tx",
    render: (r) =>
      r.tx_hash ? <code>{String(r.tx_hash).slice(0, 14)}…</code> : "—",
  },
];

const positionColumns: Column<WalletActivityItem>[] = [
  {
    key: "market",
    header: "Market",
    render: (r) => <span className="q">{r.market ?? "—"}</span>,
  },
  { key: "outcome", header: "Outcome", render: (r) => r.outcome ?? "—" },
  { key: "size", header: "Size", render: (r) => r.size ?? "—", className: "num" },
  { key: "avg", header: "Avg", render: (r) => price(r.avg_price), className: "num" },
  {
    key: "cur",
    header: "Current",
    render: (r) => price(r.current_price),
    className: "num",
  },
  { key: "pnl", header: "PnL", render: (r) => money(r.pnl), className: "num" },
];

export default function Wallets() {
  const { sampleMode } = useData();
  const [address, setAddress] = useState("");
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [registered, setRegistered] = useState<WatchWallet[]>([]);
  const [active, setActive] = useState<WalletActivity | null>(null);
  const [activityError, setActivityError] = useState<string | null>(null);
  const [loadingActivity, setLoadingActivity] = useState(false);

  async function loadActivity(addr: string) {
    setLoadingActivity(true);
    setActivityError(null);
    try {
      const res = await apiGet<WalletActivity>(
        `/wallets/${encodeURIComponent(addr)}/activity`,
      );
      setActive(res);
    } catch (err) {
      setActivityError(err instanceof Error ? err.message : String(err));
      setActive(null);
    } finally {
      setLoadingActivity(false);
    }
  }

  async function onSubmit(ev: FormEvent) {
    ev.preventDefault();
    setFormError(null);
    setBusy(true);
    try {
      const res = await apiPostJson<{ created: boolean; wallet: WatchWallet }>(
        "/wallets",
        { address, label },
      );
      setRegistered((list) => [
        res.wallet,
        ...list.filter((w) => w.address !== res.wallet.address),
      ]);
      setAddress("");
      setLabel("");
      await loadActivity(res.wallet.address);
    } catch (err) {
      if (err instanceof ApiError && err.detail?.message) {
        setFormError(String(err.detail.message));
      } else {
        setFormError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setBusy(false);
    }
  }

  if (sampleMode) {
    return (
      <div className="panel">
        <h2 className="panel-title">Wallets — watch-only</h2>
        <div className="empty">
          SAMPLE mode — no synthetic wallets or activity. Turn SAMPLE DATA off
          to use the watch-only registry against the paper backend.
        </div>
      </div>
    );
  }

  return (
    <div>
      <div className="panel">
        <h2 className="panel-title">Wallets — watch-only registry</h2>
        <p className="section-note">
          Add a public Polygon address to watch. Hunchfall never accepts
          private keys or mnemonics; watch-only means read-only.
        </p>
        <form className="wallet-form" onSubmit={onSubmit}>
          <div className="form-row">
            <label className="form-label" htmlFor="wallet-address">
              Address
            </label>
            <input
              id="wallet-address"
              className="form-input"
              placeholder="0x… (40 hex chars)"
              value={address}
              onChange={(e) => setAddress(e.target.value)}
              autoComplete="off"
              spellCheck={false}
              required
            />
          </div>
          <div className="form-row">
            <label className="form-label" htmlFor="wallet-label">
              Label
            </label>
            <input
              id="wallet-label"
              className="form-input"
              placeholder="e.g. Ikio main"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              autoComplete="off"
              required
            />
          </div>
          <button className="btn-primary" type="submit" disabled={busy}>
            {busy ? "Registering…" : "Add watch-only wallet"}
          </button>
        </form>
        {formError && (
          <div className="error-text" role="alert">
            {formError}
          </div>
        )}
        {registered.length > 0 && (
          <div className="registered-list">
            <div className="section-note">
              Registered this session (no list endpoint yet — the registry
              persists server-side):
            </div>
            {registered.map((w) => (
              <button
                key={w.address}
                className="wallet-chip"
                onClick={() => loadActivity(w.address)}
                title="Load read-only activity"
              >
                {w.label} · <code>{w.address}</code>
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="panel">
        <h2 className="panel-title">Activity — read-only</h2>
        {loadingActivity && <div className="empty">Loading…</div>}
        {activityError && (
          <div className="error-text" role="alert">
            {activityError}
          </div>
        )}
        {!loadingActivity && !activityError && !active && (
          <div className="empty">
            Register a wallet (or pick one above) to load its read-only
            activity. Nothing here can move funds — it is a GET-only view.
          </div>
        )}
        {active && (
          <>
            <div className="wallet-meta">
              <span className="chip chip-yes">watch-only</span>
              <b>{active.wallet.label}</b> <code>{active.wallet.address}</code>
              <span className="muted">fetched {fmtTs(active.fetched_at)}</span>
            </div>
            <div className="wallet-meta">
              sources: {active.sources.join(", ") || "none"}
              {active.degraded.length > 0 && (
                <span className="tone-amber">
                  degraded: {active.degraded.join(", ")}
                </span>
              )}
            </div>
            {active.chain && (
              <div className="wallet-meta">
                <span>
                  POL balance <b>{active.chain.balance_pol ?? "—"}</b>
                </span>
                <span>
                  nonce <b>{active.chain.nonce ?? "—"}</b>
                </span>
                <span className="muted">{active.chain.rpc_url}</span>
              </div>
            )}
            {active.note && <div className="section-note">{active.note}</div>}
            <DataTable
              columns={activityColumns}
              rows={active.activity}
              rowKey={(_, i) => `activity-${i}`}
              emptyText="No activity found."
            />
            {active.positions.length > 0 && (
              <DataTable
                columns={positionColumns}
                rows={active.positions}
                rowKey={(_, i) => `position-${i}`}
                emptyText="No open positions."
              />
            )}
          </>
        )}
      </div>
    </div>
  );
}
