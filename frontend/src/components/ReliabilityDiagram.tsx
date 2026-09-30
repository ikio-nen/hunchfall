import {
  CartesianGrid,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { CalibrationBin } from "../api/client";

interface ReliabilityDiagramProps {
  bins: CalibrationBin[];
}

export default function ReliabilityDiagram({ bins }: ReliabilityDiagramProps) {
  const scatter = bins.map((b) => ({
    x: b.mean_pred,
    y: b.empirical,
    n: b.n,
    mid: b.bin_mid,
  }));
  const ideal = [
    { x: 0, y: 0 },
    { x: 1, y: 1 },
  ];

  return (
    <div className="panel">
      <h2 className="panel-title">Reliability Diagram — predicted vs empirical P(true)</h2>
      {scatter.length === 0 ? (
        <div className="empty">No calibration bins.</div>
      ) : (
        <ResponsiveContainer width="100%" height={280}>
          <ScatterChart margin={{ top: 8, right: 12, bottom: 8, left: 4 }}>
            <CartesianGrid stroke="#1f2530" strokeDasharray="3 3" />
            <XAxis
              type="number"
              dataKey="x"
              domain={[0, 1]}
              tick={{ fill: "#9aa0a6", fontSize: 11 }}
              stroke="#2a3140"
              label={{ value: "Predicted P(true)", fill: "#9aa0a6", fontSize: 11, position: "insideBottom", offset: -4 }}
            />
            <YAxis
              type="number"
              dataKey="y"
              domain={[0, 1]}
              tick={{ fill: "#9aa0a6", fontSize: 11 }}
              stroke="#2a3140"
              label={{ value: "Empirical rate", fill: "#9aa0a6", fontSize: 11, angle: -90, position: "insideLeft" }}
              width={60}
            />
            <Tooltip
              contentStyle={{
                background: "#14161c",
                border: "1px solid #2a3140",
                color: "#e8eaed",
              }}
              formatter={(v, name, item) => {
                const p = item?.payload as { x: number; y: number; n: number } | undefined;
                return [
                  `pred ${p?.x.toFixed(2)} / emp ${p?.y.toFixed(2)} (n=${p?.n})`,
                  "bin",
                ];
              }}
              labelFormatter={() => ""}
            />
            <ReferenceLine segment={ideal} stroke="#5a6270" strokeDasharray="6 4" />
            <Scatter name="bins" data={scatter} fill="#b5f13c" />
            <Line dataKey="y" data={ideal} stroke="transparent" />
          </ScatterChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
