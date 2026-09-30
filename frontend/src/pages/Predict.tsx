import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import type {
  PredictAccuracy,
  PredictResponse,
  PredictResolution,
} from "../api/client";
import { ApiError, apiGet, apiPostJson } from "../api/client";
import PaperPredictionBadge from "../components/PaperPredictionBadge";
import { useData } from "../state/dataMode";

const pct = (n?: number | null) =>
  typeof n === "number" && Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : "—";
const num4 = (n?: number | null) =>
  typeof n === "number" && Number.isFinite(n) ? n.toFixed(4) : "—";
const money = (n?: number | null) =>
  typeof n === "number" && Number.isFinite(n) ? `$${n.toFixed(2)}` : "—";

function directionChip(direction: string) {
  const cls =
    direction === "YES"
      ? "chip chip-yes"
      : direction === "NO"
        ? "chip chip-no"
        : "chip chip-abstain";
  return <span className={cls}>{direction}</span>;
}

/** A prediction result card — shared by the demo and live paths. */
function ResultCard({
  result,
  resolveState,
  onResolve,
}: {
  result: PredictResponse;
  resolveState: { busy: boolean; message: string | null };
  onResolve?: (outcome: "YES" | "NO") => void;
}) {
  const { prediction, market, model, snapshot } = result;
  const price = result.market_price;
  const barPct = Math.max(0, Math.min(100, prediction.p_yes * 100));
  const pricePct = Math.max(0, Math.min(100, price * 100));
  const resolvable = result.audit_event_id !== null && !result.demo;

  return (
    <div className="pred-card">
      <div className="pred-head">
        <span className="q">{market.question}</span>
        <PaperPredictionBadge modelMock={model.mock} />
      </div>

      <div className="pred-meta">
        {directionChip(prediction.direction)}
        <span>
          P(YES) <b>{num4(prediction.p_yes)}</b>
        </span>
        <span>
          market <b>{num4(price)}</b>
        </span>
        <span>
          edge <b>{prediction.edge_vs_market >= 0 ? "+" : ""}{num4(prediction.edge_vs_market)}</b>
        </span>
        <span>
          confidence <b>{num4(prediction.confidence)}</b>
        </span>
        <span className="muted">threshold {num4(result.abstain_threshold)}</span>
        {result.snapshot_source && (
          <span className="muted">snapshot · {result.snapshot_source}</span>
        )}
      </div>

      <div className="pred-bar" aria-label="P(YES) versus market price">
        <div className="pred-bar-model" style={{ width: `${barPct}%` }} />
        <div className="pred-bar-market" style={{ left: `${pricePct}%` }} />
      </div>
      <div className="pred-bar-legend muted">
        model P(YES) {pct(prediction.p_yes)} · market {pct(price)}
      </div>

      {prediction.abstained && (
        <div className="pred-abstain" role="status">
          {`no guess — edge ${Math.abs(prediction.edge_vs_market).toFixed(3)} vs threshold ${result.abstain_threshold.toFixed(3)}`}
        </div>
      )}

      <div className="pred-reasons">
        <div className="section-note">Reasons (typed — never model prose)</div>
        <ul>
          {prediction.reasons.map((reason, i) => (
            <li key={i}>{reason}</li>
          ))}
        </ul>
      </div>

      <div className="pred-foot muted">
        <span>
          model <b>{model.version}</b>
          {model.mock && <span className="chip chip-abstain badge-chip">MOCK</span>}
          {" · ensemble "}
          <b>{model.ensemble_n}</b>
          {" · disagreement "}
          <b>{num4(model.disagreement)}</b>
        </span>
        {model.mock && <span>{model.note}</span>}
        <span>
          24h vol {money(market.volume24hr_usd)} · resolves in{" "}
          {market.hours_to_resolution != null
            ? `${market.hours_to_resolution.toFixed(1)}h`
            : "—"}
        </span>
        {snapshot.missing.length > 0 && (
          <span className="tone-amber">missing: {snapshot.missing.join(", ")}</span>
        )}
        <span className="muted">snapshot sha {snapshot.sha256.slice(0, 12)}…</span>
      </div>

      {resolvable && onResolve && (
        <div className="verdict-actions">
          <button
            className="btn-primary"
            disabled={resolveState.busy}
            onClick={() => onResolve("YES")}
          >
            Resolved YES
          </button>
          <button
            className="btn-primary"
            disabled={resolveState.busy}
            onClick={() => onResolve("NO")}
          >
            Resolved NO
          </button>
          {resolveState.message && (
            <span className="section-note">{resolveState.message}</span>
          )}
        </div>
      )}
    </div>
  );
}

export default function Predict() {
  const { sampleMode, fetchOne } = useData();
  const [slug, setSlug] = useState("");
  const [demo, setDemo] = useState<PredictResponse | null>(null);
  const [result, setResult] = useState<PredictResponse | null>(null);
  const [accuracy, setAccuracy] = useState<PredictAccuracy | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resolveState, setResolveState] = useState<{
    busy: boolean;
    message: string | null;
  }>({ busy: false, message: null });

  const loadAccuracy = useCallback(async () => {
    try {
      setAccuracy(await apiGet<PredictAccuracy>("/predict/accuracy"));
    } catch {
      setAccuracy(null);
    }
  }, []);

  useEffect(() => {
    if (sampleMode) {
      setLoading(false);
      return;
    }
    let alive = true;
    setLoading(true);
    // The demo endpoint is the sample path: it works with zero setup.
    fetchOne<PredictResponse>("/predict/demo", {} as PredictResponse)
      .then((res) => {
        if (!alive) return;
        setDemo(res);
      })
      .catch(() => {})
      .finally(() => alive && setLoading(false));
    loadAccuracy();
    return () => {
      alive = false;
    };
  }, [sampleMode, fetchOne, loadAccuracy]);

  async function onSubmit(ev: FormEvent) {
    ev.preventDefault();
    setError(null);
    setBusy(true);
    setResolveState({ busy: false, message: null });
    try {
      const res = await apiPostJson<PredictResponse>("/predict", {
        market_slug: slug.trim(),
      });
      setResult(res);
      await loadAccuracy();
    } catch (err) {
      if (err instanceof ApiError && err.detail?.message) {
        setError(String(err.detail.message));
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setBusy(false);
    }
  }

  async function onResolve(outcome: "YES" | "NO") {
    if (!result) return;
    setResolveState({ busy: true, message: null });
    try {
      const res = await apiPostJson<PredictResolution>(
        `/predict/${encodeURIComponent(result.prediction.prediction_id)}/resolve`,
        { outcome },
      );
      setResolveState({
        busy: false,
        message: `resolved ${res.outcome} — model Brier ${num4(res.brier.model)} vs market ${num4(res.brier.market)}`,
      });
      await loadAccuracy();
    } catch (err) {
      const message =
        err instanceof ApiError && err.detail?.message
          ? String(err.detail.message)
          : err instanceof Error
            ? err.message
            : String(err);
      setResolveState({ busy: false, message });
    }
  }

  if (sampleMode) {
    return (
      <div className="panel">
        <h2 className="panel-title">Predict — market guesser</h2>
        <div className="empty">
          SAMPLE mode — no synthetic predictions or accuracy numbers. Turn
          SAMPLE DATA off to use the paper backend (the demo endpoint needs no
          API key).
        </div>
      </div>
    );
  }

  return (
    <div>
      <div className="panel">
        <h2 className="panel-title">
          Predict — market guesser (prediction only)
        </h2>
        <p className="section-note">
          Given a Polymarket market, hunchfall guesses P(YES), a direction and a
          confidence — or abstains when the model adds less than 10% over the
          market. No gate, no sizing, no fills: the loop and the paper engine
          are untouched.
        </p>

        <form className="wallet-form" onSubmit={onSubmit}>
          <div className="form-row">
            <label className="form-label" htmlFor="predict-slug">
              Market slug
            </label>
            <input
              id="predict-slug"
              className="form-input"
              placeholder="e.g. will-bitcoin-hit-100k-in-2026"
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              autoComplete="off"
              spellCheck={false}
              required
            />
          </div>
          <button className="btn-primary" type="submit" disabled={busy}>
            {busy ? "Guessing…" : "Guess this market"}
          </button>
        </form>
        {error && (
          <div className="error-text" role="alert">
            {error}
          </div>
        )}
      </div>

      {result && (
        <div className="panel">
          <h2 className="panel-title">Prediction</h2>
          <ResultCard
            result={result}
            resolveState={resolveState}
            onResolve={onResolve}
          />
        </div>
      )}

      <div className="panel">
        <h2 className="panel-title">Demo — zero setup</h2>
        {loading ? (
          <div className="empty">Loading…</div>
        ) : demo && demo.market ? (
          <ResultCard result={demo} resolveState={{ busy: false, message: null }} />
        ) : (
          <div className="empty">
            No demo available — is the paper backend running? The demo falls
            back to a committed canned snapshot and never persists.
          </div>
        )}
      </div>

      <div className="panel">
        <h2 className="panel-title">Accuracy — derived on read</h2>
        {!accuracy ? (
          <div className="empty">No accuracy data yet.</div>
        ) : (
          <>
            <div className="stats-grid">
              <div className="stat-card">
                <div className="stat-label">logged</div>
                <div className="stat-value">{accuracy.n_logged}</div>
              </div>
              <div className="stat-card">
                <div className="stat-label">resolved</div>
                <div className="stat-value">
                  {accuracy.n_resolved}/{accuracy.n_logged}
                </div>
              </div>
              <div className="stat-card">
                <div className="stat-label">Brier (model)</div>
                <div className="stat-value">
                  {num4(accuracy.brier?.model ?? null)}
                </div>
              </div>
              <div className="stat-card">
                <div className="stat-label">Brier (market)</div>
                <div className="stat-value">
                  {num4(accuracy.brier?.market ?? null)}
                </div>
              </div>
              <div className="stat-card">
                <div className="stat-label">skill vs market</div>
                <div className="stat-value">
                  {num4(accuracy.brier?.skill_vs_market ?? null)}
                </div>
              </div>
              <div className="stat-card">
                <div className="stat-label">direction acc.</div>
                <div className="stat-value">{pct(accuracy.direction_accuracy)}</div>
              </div>
              <div className="stat-card">
                <div className="stat-label">abstention rate</div>
                <div className="stat-value">{pct(accuracy.abstention_rate)}</div>
              </div>
            </div>
            <div className="section-note">
              Benchmarks: always-0.5 Brier is {num4(accuracy.brier?.baseline_0_5 ?? 0.25)}.
              Mock predictions are {accuracy.include_mock ? "included" : "excluded"}{" "}
              by default — mock runs can never inflate accuracy. Split: live{" "}
              {accuracy.mock_split.live?.n_logged ?? 0} logged /{" "}
              {accuracy.mock_split.live?.n_resolved ?? 0} resolved, mock{" "}
              {accuracy.mock_split.mock?.n_logged ?? 0} logged /{" "}
              {accuracy.mock_split.mock?.n_resolved ?? 0} resolved.
            </div>
            {accuracy.n_logged === 0 && (
              <div className="empty">
                No predictions logged yet. Guess a market above, then resolve it
                when the outcome is known — calibration appears here.
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
