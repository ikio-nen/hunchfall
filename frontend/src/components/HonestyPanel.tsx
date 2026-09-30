import { useEffect, useState } from "react";
import { sampleFills, sampleVetoes } from "../api/sampleData";
import { useData } from "../state/dataMode";
import type { Fill, Veto } from "../api/client";

function usd(n: number): string {
  return `$${n.toFixed(2)}`;
}

/**
 * HonestyPanel — always-visible footer strip.
 * Never fakes anything: totals come from /fills and /vetoes (or SAMPLE data).
 */
export default function HonestyPanel() {
  const { sampleMode, fetchList } = useData();
  const [fills, setFills] = useState<Fill[]>([]);
  const [vetoes, setVetoes] = useState<Veto[]>([]);

  useEffect(() => {
    let alive = true;
    Promise.all([
      fetchList<Fill>("/fills", sampleFills),
      fetchList<Veto>("/vetoes", sampleVetoes),
    ])
      .then(([f, v]) => {
        if (alive) {
          setFills(f);
          setVetoes(v);
        }
      })
      .catch(() => {
        /* stays at zero rather than inventing numbers */
      });
    return () => {
      alive = false;
    };
  }, [fetchList, sampleMode]);

  const totalFees = fills.reduce((s, f) => s + f.fee_usd, 0);
  const totalSlippage = fills.reduce((s, f) => s + f.slippage_usd, 0);
  // Losing fills: fills give no realized PnL, so we use a transparent,
  // cost-based rule — a fill counts as losing when its execution costs
  // (fee + slippage) exceed 1% of its notional size. Rule is stated in
  // the title tooltip and README; never invented PnL.
  const losingFills = fills.filter(
    (f) => f.fee_usd + f.slippage_usd > 0.01 * f.size_usd,
  ).length;
  const vetoCount = vetoes.length;

  const cells: Array<[string, string, string?]> = [
    ["Fees paid", usd(totalFees), ""],
    ["Slippage", usd(totalSlippage), totalSlippage > 0 ? "amber" : ""],
    ["Losing fills", String(losingFills), losingFills > 0 ? "red" : ""],
    ["Signals vetoed", String(vetoCount), vetoCount > 0 ? "amber" : ""],
    ["Fills (paper)", String(fills.length), ""],
  ];  return (
    <footer className="honesty-bar" aria-label="Honesty panel">
      <span className="honesty-title">HONESTY</span>
      {cells.map(([label, value, tone]) => (
        <span
          key={label}
          className="honesty-cell"
          title={
            label === "Losing fills"
              ? "Fills with no realized PnL; counted as losing when fee+slippage > 1% of size. Rule, not invented PnL."
              : undefined
          }
        >
          <span className="honesty-label">{label}</span>
          <span className={`honesty-value${tone ? ` tone-${tone}` : ""}`}>
            {value}
          </span>
        </span>
      ))}
      <span className="honesty-note">
        {sampleMode
          ? "SAMPLE — synthetic demo data"
          : "live backend · all figures are paper, none real"}
      </span>
      <span className="honesty-note disclosure" title="Paper-trading disclosure">
        Paper simulation on public market data. No orders were placed; no wallet
        was connected.
      </span>
    </footer>
  );
}
