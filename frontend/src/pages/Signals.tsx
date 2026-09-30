import { useCallback, useEffect, useState } from "react";
import type { Signal } from "../api/client";
import { ApiError, apiPostJson, toSignalRows } from "../api/client";
import { sampleSignals } from "../api/sampleData";
import { useData } from "../state/dataMode";
import DataTable, { type Column } from "../components/DataTable";
import PaperPredictionBadge from "../components/PaperPredictionBadge";

function fmtTs(ts: string): string {
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts : d.toLocaleString([], { hour12: false });
}

function decisionChip(d: string): string {
  const up = d.toUpperCase();
  if (up === "TRADE" || up === "YES") return "chip chip-yes";
  if (up === "ABSTAIN" || up === "SKIP") return "chip chip-abstain";
  return "chip chip-no";
}

function signalRef(s: Signal): string | null {
  if (s.signal_id) return s.signal_id;
  if (s.id !== undefined) return String(s.id);
  return null;
}

export default function Signals() {
  const { sampleMode, fetchList } = useData();
  const [rows, setRows] = useState<Signal[]>([]);
  const [loading, setLoading] = useState(true);
  const [verdicts, setVerdicts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    fetchList<unknown>("/signals", sampleSignals)
      .then((raw) => {
        if (!alive) return;
        setRows(sampleMode ? (raw as Signal[]) : toSignalRows(raw));
      })
      .catch(() => {})
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [fetchList, sampleMode]);

  const validate = useCallback(
    async (row: Signal, outcome: "HIT" | "MISS") => {
      const ref = signalRef(row);
      if (!ref || sampleMode) return;
      setBusy(`${ref}:${outcome}`);
      try {
        await apiPostJson(`/signals/${encodeURIComponent(ref)}/validate`, {
          outcome,
        });
        setVerdicts((v) => ({ ...v, [ref]: outcome }));
      } catch (err) {
        if (err instanceof ApiError && err.detail?.code === "already_validated") {
          setVerdicts((v) => ({ ...v, [ref]: "already validated" }));
        } else if (err instanceof ApiError && err.detail?.message) {
          const message = String(err.detail.message);
          setVerdicts((v) => ({ ...v, [ref]: message }));
        } else {
          setVerdicts((v) => ({
            ...v,
            [ref]: err instanceof Error ? err.message : "validation failed",
          }));
        }
      } finally {
        setBusy(null);
      }
    },
    [sampleMode],
  );

  const columns: Column<Signal>[] = [
    { key: "ts", header: "Time", render: (s) => fmtTs(s.ts) },
    {
      key: "badge",
      header: "Label",
      render: (s) => (
        <PaperPredictionBadge modelMock={s.model_mock} sample={s.sample} />
      ),
    },
    {
      key: "q",
      header: "Question",
      render: (s) => <span className="q">{s.question}</span>,
    },
    {
      key: "marketplace",
      header: "Marketplace",
      render: (s) => s.marketplace ?? "polymarket",
    },
    {
      key: "side",
      header: "Side",
      render: (s) =>
        s.side ? (
          <span className={`chip ${s.side === "YES" ? "chip-yes" : "chip-no"}`}>
            {s.side}
          </span>
        ) : (
          "—"
        ),
    },
    {
      key: "p",
      header: "P(true)",
      render: (s) => s.p_true.toFixed(2),
      className: "num",
    },
    {
      key: "mkt",
      header: "Market ¢",
      render: (s) => s.market_price.toFixed(2),
      className: "num",
    },
    {
      key: "edge",
      header: "Edge",
      render: (s) => (
        <span
          className={
            s.edge >= 0.1 ? "tone-green" : s.edge >= 0 ? "tone-amber" : "tone-red"
          }
        >
          {s.edge >= 0 ? "+" : ""}
          {s.edge.toFixed(2)}
        </span>
      ),
      className: "num",
    },
    {
      key: "decision",
      header: "Decision",
      render: (s) => (
        <span className={decisionChip(s.decision)}>{s.decision}</span>
      ),
    },
    {
      key: "validate",
      header: "Verdict",
      render: (s) => {
        if (sampleMode) {
          return <span className="muted">sample mode — disabled</span>;
        }
        const ref = signalRef(s);
        if (!ref) return <span className="muted">no id</span>;
        const verdict = verdicts[ref];
        if (verdict === "HIT" || verdict === "MISS") {
          return (
            <span className={`chip ${verdict === "HIT" ? "chip-yes" : "chip-no"}`}>
              {verdict}
            </span>
          );
        }
        if (verdict) return <span className="muted">{verdict}</span>;
        return (
          <span className="verdict-actions">
            <button
              className="btn-mini"
              onClick={() => validate(s, "HIT")}
              disabled={busy !== null}
            >
              HIT
            </button>
            <button
              className="btn-mini"
              onClick={() => validate(s, "MISS")}
              disabled={busy !== null}
            >
              MISS
            </button>
          </span>
        );
      },
    },
  ];

  return (
    <div className="panel">
      <h2 className="panel-title">Signals — paper predictions only</h2>
      <p className="section-note">
        Every row is a paper prediction — no trade was placed. HIT/MISS
        verdicts are recorded against the stored signal and feed the
        calibration chart (Prove).
      </p>
      {loading ? (
        <div className="empty">Loading…</div>
      ) : (
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(row, i) => row.signal_id ?? `signal-${row.id ?? i}`}
          emptyText="No signals."
        />
      )}
    </div>
  );
}
