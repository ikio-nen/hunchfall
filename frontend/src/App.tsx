import { useState } from "react";
import Nav, { type TabId } from "./components/Nav";
import HonestyPanel from "./components/HonestyPanel";
import Overview from "./pages/Overview";
import Positions from "./pages/Positions";
import Signals from "./pages/Signals";
import Vetoes from "./pages/Vetoes";
import Fills from "./pages/Fills";
import Calibration from "./pages/Calibration";
import { useData } from "./state/dataMode";

export default function App() {
  const [tab, setTab] = useState<TabId>("overview");
  const { sampleMode, toggleSampleMode, backendDown } = useData();

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-name">hunchfall</span>
          <span className="paper-badge">PAPER</span>
          <span
            className="disclosure"
            title="This dashboard shows a paper-trading simulation only"
          >
            PAPER TRADING SIMULATION. Not financial advice.
          </span>
          <span className={`status-dot${backendDown && !sampleMode ? " down" : ""}`}
            title={backendDown && !sampleMode ? "Backend unreachable" : "Data source OK"}
          />
        </div>

        <div className="topbar-right">
          <label className="sample-toggle" title="Swap live backend for clearly-labeled synthetic demo data">
            <span className="toggle-label">SAMPLE DATA</span>
            <button
              role="switch"
              aria-checked={sampleMode}
              aria-label="Toggle sample data mode"
              className={`switch${sampleMode ? " on" : ""}`}
              onClick={toggleSampleMode}
            >
              <span className="knob" />
            </button>
          </label>
        </div>
      </header>

      {sampleMode && (
        <div className="sample-banner" role="status">
          SAMPLE — synthetic demo data. Not real market data, not real trades.
        </div>
      )}
      {backendDown && !sampleMode && (
        <div className="error-banner" role="alert">
          Backend unreachable — check VITE_API_BASE. Enable SAMPLE DATA to demo without
          live APIs.
        </div>
      )}

      <Nav active={tab} onChange={setTab} />

      <main className="content">
        {tab === "overview" && <Overview />}
        {tab === "positions" && <Positions />}
        {tab === "signals" && <Signals />}
        {tab === "vetoes" && <Vetoes />}
        {tab === "fills" && <Fills />}
        {tab === "calibration" && <Calibration />}
      </main>

      <HonestyPanel />
    </div>
  );
}
