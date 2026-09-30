import { useState } from "react";
import { apiPost, API_BASE } from "../api/client";
import { useData } from "../state/dataMode";

interface KillButtonProps {
  engaged: boolean;
  onToggle: () => void;
}

export default function KillButton({ engaged, onToggle }: KillButtonProps) {
  const { sampleMode } = useData();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleKill() {
    const ok = window.confirm(
      "ENGAGE KILL SWITCH?\n\nThis halts all new paper trades immediately.\nOpen positions stay as-is.",
    );
    if (!ok) return;
    setError(null);
    if (sampleMode) {
      // No real backend in sample mode — simulate locally so the demo flows.
      onToggle();
      return;
    }
    setBusy(true);
    try {
      await apiPost("/kill");
      onToggle();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function handleResume() {
    setError(null);
    if (sampleMode) {
      onToggle();
      return;
    }
    setBusy(true);
    try {
      await apiPost("/resume");
      onToggle();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="kill-wrap">
      {engaged ? (
        <button
          className="kill-btn kill-btn-engaged"
          onClick={handleResume}
          disabled={busy}
        >
          {busy ? "…" : "RESUME — kill switch engaged"}
        </button>
      ) : (
        <button className="kill-btn" onClick={handleKill} disabled={busy}>
          {busy ? "…" : "⛔ KILL SWITCH"}
        </button>
      )}
      {sampleMode && (
        <div className="kill-note">
          sample mode — no real backend call (would POST {API_BASE}/kill)
        </div>
      )}
      {error && <div className="kill-error">{error}</div>}
    </div>
  );
}
