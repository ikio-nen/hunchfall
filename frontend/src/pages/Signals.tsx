import { useEffect, useState } from "react";
import type { Signal } from "../api/client";
import { sampleSignals } from "../api/sampleData";
import { useData } from "../state/dataMode";
import DataTable, { type Column } from "../components/DataTable";

function fmtTs(ts: string): string {
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts : d.toLocaleString([], { hour12: false });
}

function decisionChip(d: string): string {
  const up = d.toUpperCase();
  if (up === "TRADE") return "chip chip-yes";
  if (up === "ABSTAIN") return "chip chip-abstain";
  return "chip chip-no";
}

const columns: Column<Signal>[] = [
  { key: "ts", header: "Time", render: (s) => fmtTs(s.ts) },
  { key: "q", header: "Question", render: (s) => <span className="q">{s.question}</span> },
  { key: "p", header: "P(true)", render: (s) => s.p_true.toFixed(2), className: "num" },
  { key: "mkt", header: "Market ¢", render: (s) => s.market_price.toFixed(2), className: "num" },
  {
    key: "edge",
    header: "Edge",
    render: (s) => (
      <span className={s.edge >= 0.1 ? "tone-green" : s.edge >= 0 ? "tone-amber" : "tone-red"}>
        {s.edge >= 0 ? "+" : ""}
        {s.edge.toFixed(2)}
      </span>
    ),
    className: "num",
  },
  {
    key: "decision",
    header: "Decision",
    render: (s) => <span className={decisionChip(s.decision)}>{s.decision}</span>,
  },
];

export default function Signals() {
  const { sampleMode, fetchList } = useData();
  const [rows, setRows] = useState<Signal[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    fetchList<Signal>("/signals", sampleSignals)
      .then((r) => alive && setRows(r))
      .catch(() => {})
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [fetchList, sampleMode]);

  return (
    <div className="panel">
      <h2 className="panel-title">Signals — model P(true) vs market price</h2>
      {loading ? (
        <div className="empty">Loading…</div>
      ) : (
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(_, i) => `signal-${i}`}
          emptyText="No signals."
        />
      )}
    </div>
  );
}
