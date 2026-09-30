import { useEffect, useState } from "react";
import type { Position } from "../api/client";
import { samplePositions } from "../api/sampleData";
import { useData } from "../state/dataMode";
import DataTable, { type Column } from "../components/DataTable";

const pnlTone = (n: number) => (n >= 0 ? "tone-green" : "tone-red");

const columns: Column<Position>[] = [
  { key: "market_id", header: "Market ID", render: (p) => <code>{p.market_id}</code> },
  {
    key: "side",
    header: "Side",
    render: (p) => (
      <span className={`chip ${p.side === "YES" ? "chip-yes" : "chip-no"}`}>{p.side}</span>
    ),
  },
  { key: "contracts", header: "Contracts", render: (p) => String(p.contracts), className: "num" },
  { key: "avg", header: "Avg ¢", render: (p) => p.avg_price.toFixed(2), className: "num" },
  { key: "cur", header: "Current ¢", render: (p) => p.current_price.toFixed(2), className: "num" },
  {
    key: "pnl",
    header: "Unrealized PnL",
    render: (p) => (
      <span className={pnlTone(p.unrealized_pnl)}>
        {p.unrealized_pnl >= 0 ? "+" : ""}${p.unrealized_pnl.toFixed(2)}
      </span>
    ),
    className: "num",
  },
];

export default function Positions() {
  const { sampleMode, fetchList } = useData();
  const [rows, setRows] = useState<Position[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    fetchList<Position>("/positions", samplePositions)
      .then((r) => alive && setRows(r))
      .catch(() => {})
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [fetchList, sampleMode]);

  return (
    <div className="panel">
      <h2 className="panel-title">Open Positions — PAPER</h2>
      {loading ? (
        <div className="empty">Loading…</div>
      ) : (
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(r, i) => `${r.market_id}-${r.side}-${i}`}
          emptyText="No open positions."
        />
      )}
    </div>
  );
}
