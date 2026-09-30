import { useEffect, useState } from "react";
import type { CalibrationBin, CalibrationReport } from "../api/client";
import { EMPTY_CALIBRATION, toCalibrationReport } from "../api/client";
import { sampleCalibration } from "../api/sampleData";
import { useData } from "../state/dataMode";
import ReliabilityDiagram from "../components/ReliabilityDiagram";
import StatCard from "../components/StatCard";
import DataTable, { type Column } from "../components/DataTable";

const binColumns: Column<CalibrationBin>[] = [
  { key: "mid", header: "Bin center", render: (b) => b.bin_mid.toFixed(2), className: "num" },
  { key: "pred", header: "Mean predicted", render: (b) => b.mean_pred.toFixed(2), className: "num" },
  { key: "emp", header: "Empirical rate", render: (b) => b.empirical.toFixed(2), className: "num" },
  { key: "n", header: "n", render: (b) => String(b.n), className: "num" },
];

const pct = (n: number | null) => (n === null ? "—" : `${(n * 100).toFixed(1)}%`);

export default function Calibration() {
  const { sampleMode, fetchOne } = useData();
  const [report, setReport] = useState<CalibrationReport>(sampleCalibration);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    if (sampleMode) {
      setReport(sampleCalibration);
      setLoading(false);
      return () => {
        alive = false;
      };
    }
    setLoading(true);
    fetchOne<unknown>("/calibration", null)
      .then((raw) => {
        if (alive) setReport(toCalibrationReport(raw, EMPTY_CALIBRATION));
      })
      .catch(() => {})
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [fetchOne, sampleMode]);

  if (loading) {
    return (
      <div className="panel">
        <div className="empty">Loading…</div>
      </div>
    );
  }

  return (
    <div>
      <div className="stats-grid">
        <StatCard label="Accuracy" value={pct(report.accuracy)} tone="green" />
        <StatCard label="Abstention rate" value={pct(report.abstention_rate)} />
        <StatCard
          label="Brier score"
          value={report.brier === null ? "—" : report.brier.toFixed(3)}
        />
        <StatCard
          label="Validated"
          value={String(report.n_validated ?? report.bins.reduce((s, b) => s + b.n, 0))}
        />
      </div>

      <ReliabilityDiagram bins={report.bins} />

      <div className="panel">
        <h2 className="panel-title">Bins</h2>
        <DataTable
          columns={binColumns}
          rows={report.bins}
          rowKey={(b, i) => `bin-${b.bin_mid}-${i}`}
          emptyText="No calibration bins — no validated hunches yet."
        />
      </div>
    </div>
  );
}
