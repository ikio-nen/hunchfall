import { useEffect, useState } from "react";
import type { Position, Status } from "../api/client";
import { apiList } from "../api/client";
import { sampleEquityCurve, samplePositions, sampleStatus } from "../api/sampleData";
import { useData } from "../state/dataMode";
import EquityChart from "../components/EquityChart";
import StatCard from "../components/StatCard";
import KillButton from "../components/KillButton";
import type { EquityPoint } from "../api/sampleData";

const usd = (n: number) => `$${n.toFixed(2)}`;

export default function Overview() {
  const { sampleMode, fetchList, fetchOne } = useData();
  const [status, setStatus] = useState<Status>(sampleStatus);
  const [positions, setPositions] = useState<Position[]>([]);
  const [curve, setCurve] = useState<EquityPoint[]>(sampleEquityCurve);
  const [loading, setLoading] = useState(true);

  const [killEngaged, setKillEngaged] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    Promise.all([
      fetchOne<Status>("/status", sampleStatus),
      fetchList<Position>("/positions", samplePositions),
      // Equity history: sample data always available; live backend may not
      // expose /equity — on failure we just show the current equity stat.
      sampleMode
        ? Promise.resolve(sampleEquityCurve)
        : apiList<EquityPoint>("/equity").catch(() => [] as EquityPoint[]),
    ])
      .then(([st, pos, eq]) => {
        if (!alive) return;
        setStatus(st);
        setPositions(pos);
        setCurve(eq.length > 0 ? eq : [{ ts: new Date().toISOString(), equity: st.equity, ...({ sample: false } as const) }]);
        setKillEngaged(st.kill_switch_engaged);
        setLoading(false);
      })
      .catch(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [fetchList, fetchOne, sampleMode]);

  const openCount = positions.length;
  const totalUnrealized = positions.reduce((s, p) => s + p.unrealized_pnl, 0);
  const mode = status.mode || "PAPER";

  return (
    <div>
      {loading ? (
        <div className="empty">Loading…</div>
      ) : (
        <>
          <div className="stats-grid">
            <StatCard label="Bankroll" value={usd(status.bankroll)} />
            <StatCard
              label="Equity"
              value={usd(status.equity)}
              tone={status.equity >= status.bankroll ? "green" : "red"}
            />
            <StatCard
              label="Exposure (USD)"
              value={usd(status.exposure_usd)}
              tone={status.exposure_usd > 500 ? "amber" : undefined}
            />
            <StatCard label="Open positions" value={String(openCount)} />
            <StatCard
              label="Unrealized PnL"
              value={usd(totalUnrealized)}
              tone={totalUnrealized >= 0 ? "green" : "red"}
            />
            <StatCard label="Mode" value={mode} tone={mode === "PAPER" ? "green" : "red"} />
          </div>

          <EquityChart points={curve} />

          <div className="panel">
            <h2 className="panel-title">Kill switch</h2>
            <KillButton engaged={killEngaged} onToggle={() => setKillEngaged((k) => !k)} />
          </div>
        </>
      )}
    </div>
  );
}
