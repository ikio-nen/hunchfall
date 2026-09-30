import { useEffect, useState } from "react";
import type { CalibrationReport, Fill, Veto } from "../api/client";
import { EMPTY_CALIBRATION, toCalibrationReport } from "../api/client";
import { useData } from "../state/dataMode";
import ReliabilityDiagram from "../components/ReliabilityDiagram";
import StatCard from "../components/StatCard";
import DataTable, { type Column } from "../components/DataTable";

function fmtTs(ts: string): string {
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts : d.toLocaleString([], { hour12: false });
}

const pct = (n: number | null) => (n === null ? "—" : `${(n * 100).toFixed(1)}%`);
const score = (n: number | null) => (n === null ? "—" : n.toFixed(3));

const vetoColumns: Column<Veto>[] = [
  { key: "ts", header: "Time", render: (v) => fmtTs(v.ts) },
  {
    key: "reason",
    header: "Reason",
    render: (v) => <span className="chip chip-no">{v.reason}</span>,
  },
  { key: "detail", header: "Detail", render: (v) => v.detail },
];

const fillColumns: Column<Fill>[] = [
  { key: "ts", header: "Time", render: (f) => fmtTs(f.ts) },
  {
    key: "side",
    header: "Side",
    render: (f) => (
      <span className={`chip ${f.side === "BUY" ? "chip-yes" : "chip-no"}`}>
        {f.side}
      </span>
    ),
  },
  { key: "price", header: "Price ¢", render: (f) => f.price.toFixed(2), className: "num" },
  {
    key: "size",
    header: "Size",
    render: (f) => `$${f.size_usd.toFixed(2)}`,
    className: "num",
  },
  {
    key: "fee",
    header: "Fee",
    render: (f) => `$${f.fee_usd.toFixed(3)}`,
    className: "num",
  },
];

export default function Prove() {
  const { sampleMode, fetchOne, fetchList } = useData();
  const [report, setReport] = useState<CalibrationReport>(EMPTY_CALIBRATION);
  const [vetoes, setVetoes] = useState<Veto[]>([]);
  const [fills, setFills] = useState<Fill[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    if (sampleMode) {
      setReport(EMPTY_CALIBRATION);
      setVetoes([]);
      setFills([]);
      setLoading(false);
      return () => {
        alive = false;
      };
    }
    setLoading(true);
    Promise.all([
      fetchOne<unknown>("/calibration", null),
      fetchList<Veto>("/vetoes", []),
      fetchList<Fill>("/fills", []),
    ])
      .then(([rawCalibration, vetoRows, fillRows]) => {
        if (!alive) return;
        setReport(toCalibrationReport(rawCalibration, EMPTY_CALIBRATION));
        setVetoes(vetoRows);
        setFills(fillRows);
      })
      .catch(() => {})
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [fetchList, fetchOne, sampleMode]);

  if (sampleMode) {
    return (
      <div className="panel">
        <h2 className="panel-title">Prove — calibration & audit</h2>
        <div className="empty">
          SAMPLE mode — no synthetic proof numbers. Turn SAMPLE DATA off to see
          real paper calibration, vetoes, and fills.
        </div>
      </div>
    );
  }

  return (
    <div>
      <div className="stats-grid">
        <StatCard label="Accuracy (validated)" value={pct(report.accuracy)} />
        <StatCard label="Brier score" value={score(report.brier)} />
        <StatCard
          label="Validated hunches"
          value={String(report.n_validated ?? 0)}
        />
        <StatCard label="Vetoes recorded" value={String(vetoes.length)} />
        <StatCard label="Paper fills" value={String(fills.length)} />
      </div>

      <ReliabilityDiagram bins={report.bins} />

      <div className="panel">
        <h2 className="panel-title">Tail risk — every veto, with reason</h2>
        {loading ? (
          <div className="empty">Loading…</div>
        ) : (
          <DataTable
            columns={vetoColumns}
            rows={vetoes}
            rowKey={(_, i) => `veto-${i}`}
            emptyText="No vetoes recorded."
          />
        )}
      </div>

      <div className="panel">
        <h2 className="panel-title">Paper fills — costs visible</h2>
        {loading ? (
          <div className="empty">Loading…</div>
        ) : (
          <DataTable
            columns={fillColumns}
            rows={fills}
            rowKey={(f, i) => `${f.fill_id}-${i}`}
            emptyText="No paper fills."
          />
        )}
      </div>
    </div>
  );
}
