import { useEffect, useState } from "react";
import type { EquityPoint, Position, Status } from "../api/client";
import { toEquityPoints, toStatus } from "../api/client";
import { sampleEquityCurve, samplePositions, sampleStatus } from "../api/sampleData";
import { useData } from "../state/dataMode";
import EquityChart from "../components/EquityChart";
import StatCard from "../components/StatCard";
import KillButton from "../components/KillButton";

/** Live fallback: honest nulls until the loop writes real state. */
const EMPTY_STATUS: Status = {
  bankroll: null,
  equity: null,
  exposure_usd: null,
  kill_switch_engaged: false,
  mode: "PAPER",
};

const usd = (n: number | null | undefined) =>
  typeof n === "number" && Number.isFinite(n) ? `$${n.toFixed(2)}` : "—";

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
      fetchOne<unknown>("/status", sampleStatus),
      fetchList<Position>("/positions", samplePositions),
    ])
      .then(([rawStatus, pos]) => {
        if (!alive) return;
        const st = sampleMode ? (rawStatus as Status) : toStatus(rawStatus, EMPTY_STATUS);
        const points = sampleMode ? sampleEquityCurve : toEquityPoints(rawStatus);
        setStatus(st);
        setPositions(pos);
        setCurve(
          points.length > 0
            ? points
            : typeof st.equity === "number"
              ? [{ ts: new Date().toISOString(), equity: st.equity }]
              : [],
        );
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
  const equityTone =
    typeof status.equity === "number" && typeof status.bankroll === "number"
      ? status.equity >= status.bankroll
        ? "green"
        : "red"
      : undefined;

  return (
    <div>
      {loading ? (
        <div className="empty">Loading…</div>
      ) : (
        <>
          <div className="stats-grid">
            <StatCard label="Bankroll" value={usd(status.bankroll)} />
            <StatCard label="Equity" value={usd(status.equity)} tone={equityTone} />
            <StatCard
              label="Exposure (USD)"
              value={usd(status.exposure_usd)}
              tone={
                typeof status.exposure_usd === "number" && status.exposure_usd > 500
                  ? "amber"
                  : undefined
              }
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
