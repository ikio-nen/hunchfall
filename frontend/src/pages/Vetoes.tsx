import { useEffect, useState } from "react";
import type { Veto } from "../api/client";
import { sampleVetoes } from "../api/sampleData";
import { useData } from "../state/dataMode";
import DataTable, { type Column } from "../components/DataTable";

function fmtTs(ts: string): string {
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts : d.toLocaleString([], { hour12: false });
}

const columns: Column<Veto>[] = [
  { key: "ts", header: "Time", render: (v) => fmtTs(v.ts) },
  {
    key: "reason",
    header: "Reason",
    render: (v) => <span className="chip chip-no">{v.reason}</span>,
  },
  { key: "detail", header: "Detail", render: (v) => v.detail },
];

export default function Vetoes() {
  const { sampleMode, fetchList } = useData();
  const [rows, setRows] = useState<Veto[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    fetchList<Veto>("/vetoes", sampleVetoes)
      .then((r) => alive && setRows(r))
      .catch(() => {})
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [fetchList, sampleMode]);

  return (
    <div className="panel">
      <h2 className="panel-title">Vetoes — trades the risk gate blocked</h2>
      {loading ? (
        <div className="empty">Loading…</div>
      ) : (
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(_, i) => `veto-${i}`}
          emptyText="No vetoes recorded."
        />
      )}
    </div>
  );
}
