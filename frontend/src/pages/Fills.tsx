import { useEffect, useState } from "react";
import type { Fill } from "../api/client";
import { sampleFills } from "../api/sampleData";
import { useData } from "../state/dataMode";
import DataTable, { type Column } from "../components/DataTable";

function fmtTs(ts: string): string {
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts : d.toLocaleString([], { hour12: false });
}

const columns: Column<Fill>[] = [
  { key: "id", header: "Fill ID", render: (f) => <code>{f.fill_id}</code> },
  { key: "ts", header: "Time", render: (f) => fmtTs(f.ts) },
  {
    key: "side",
    header: "Side",
    render: (f) => (
      <span className={`chip ${f.side === "BUY" ? "chip-yes" : "chip-no"}`}>{f.side}</span>
    ),
  },
  { key: "price", header: "Price ¢", render: (f) => f.price.toFixed(2), className: "num" },
  { key: "size", header: "Size (USD)", render: (f) => `$${f.size_usd.toFixed(2)}`, className: "num" },
  { key: "fee", header: "Fee (USD)", render: (f) => `$${f.fee_usd.toFixed(3)}`, className: "num" },
  { key: "slip", header: "Slippage (USD)", render: (f) => `$${f.slippage_usd.toFixed(3)}`, className: "num" },
];

export default function Fills() {
  const { sampleMode, fetchList } = useData();
  const [rows, setRows] = useState<Fill[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    fetchList<Fill>("/fills", sampleFills)
      .then((r) => alive && setRows(r))
      .catch(() => {})
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [fetchList, sampleMode]);

  return (
    <div className="panel">
      <h2 className="panel-title">Fills — paper executions with costs</h2>
      {loading ? (
        <div className="empty">Loading…</div>
      ) : (
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(f, i) => `${f.fill_id}-${i}`}
          emptyText="No fills."
        />
      )}
    </div>
  );
}
