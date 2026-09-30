import { useEffect, useState } from "react";
import type { MarketplaceCount } from "../api/client";
import { useData } from "../state/dataMode";

function fmtTs(ts?: string | null): string {
  if (!ts) return "no scans yet";
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts : `last scan ${d.toLocaleString([], { hour12: false })}`;
}

export default function Marketplaces() {
  const { sampleMode, fetchOne } = useData();
  const [rows, setRows] = useState<MarketplaceCount[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    fetchOne<{ marketplaces?: MarketplaceCount[] }>("/marketplaces", {
      marketplaces: [],
    })
      .then((res) => {
        if (!alive) return;
        setRows(Array.isArray(res.marketplaces) ? res.marketplaces : []);
      })
      .catch(() => {})
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [fetchOne, sampleMode]);

  if (sampleMode) {
    return (
      <div className="panel">
        <h2 className="panel-title">Marketplaces</h2>
        <div className="empty">
          SAMPLE mode — no synthetic marketplace numbers. Turn SAMPLE DATA off
          to read the paper backend.
        </div>
      </div>
    );
  }

  return (
    <div className="panel">
      <h2 className="panel-title">Marketplaces — scans, hunches, validations</h2>
      <p className="section-note">
        Counts come from the append-only audit log. Every hunch is a paper
        prediction — no trade placed.
      </p>
      {loading ? (
        <div className="empty">Loading…</div>
      ) : rows.length === 0 ? (
        <div className="empty">
          No marketplace activity yet. Scans arrive through the extension
          endpoint; validations through the Signals tab.
        </div>
      ) : (
        <div className="cards-grid">
          {rows.map((m) => (
            <div className="market-card" key={m.name}>
              <div className="market-card-name">{m.name}</div>
              <div className="market-card-stats">
                <span>
                  <b>{m.scans}</b> scans
                </span>
                <span>
                  <b>{m.hunches}</b> hunches
                </span>
                <span>
                  <b>{m.validated}</b> validated
                </span>
                <span>
                  <b>{m.pending_validations}</b> pending
                </span>
              </div>
              <div className="market-card-foot">{fmtTs(m.last_scan_at)}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
