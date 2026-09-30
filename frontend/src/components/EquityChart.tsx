import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { EquityPoint } from "../api/sampleData";

interface EquityChartProps {
  points: EquityPoint[];
}

function fmtTs(ts: string): string {
  const d = new Date(ts);
  return isNaN(d.getTime())
    ? ts
    : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export default function EquityChart({ points }: EquityChartProps) {
  const data = points.map((p) => ({ ...p, label: fmtTs(p.ts) }));
  const values = points.map((p) => p.equity);
  const min = Math.min(...values, 0);
  const max = Math.max(...values, 0);

  return (
    <div className="panel">
      <h2 className="panel-title">
        Equity Curve <span className="paper-inline">PAPER</span>
      </h2>
      {data.length === 0 ? (
        <div className="empty">No equity data.</div>
      ) : (
        <ResponsiveContainer width="100%" height={280}>
          <AreaChart data={data} margin={{ top: 8, right: 12, bottom: 4, left: 4 }}>
            <CartesianGrid stroke="#1f2530" strokeDasharray="3 3" />
            <XAxis
              dataKey="label"
              tick={{ fill: "#9aa0a6", fontSize: 11 }}
              stroke="#2a3140"
            />
            <YAxis
              domain={[min * 0.995, max * 1.005]}
              tick={{ fill: "#9aa0a6", fontSize: 11 }}
              stroke="#2a3140"
              tickFormatter={(v: number) => `$${Math.round(v)}`}
              width={56}
            />
            <Tooltip
              contentStyle={{
                background: "#14161c",
                border: "1px solid #2a3140",
                color: "#e8eaed",
              }}
              formatter={(v) => [`$${Number(v).toFixed(2)}`, "Equity"]}
              labelFormatter={(l) => `Time: ${l}`}
            />
            <Area
              type="monotone"
              dataKey="equity"
              stroke="#b5f13c"
              strokeWidth={2}
              fill="#b5f13c"
              fillOpacity={0.12}
            />
          </AreaChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
