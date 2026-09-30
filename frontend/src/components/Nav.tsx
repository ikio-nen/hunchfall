export const TABS = [
  { id: "overview", label: "Overview" },
  { id: "positions", label: "Positions" },
  { id: "signals", label: "Signals" },
  { id: "vetoes", label: "Vetoes" },
  { id: "fills", label: "Fills" },
  { id: "calibration", label: "Calibration" },
] as const;

export type TabId = (typeof TABS)[number]["id"];

interface NavProps {
  active: TabId;
  onChange: (tab: TabId) => void;
}

export default function Nav({ active, onChange }: NavProps) {
  return (
    <nav className="nav" role="tablist" aria-label="Paper trading sections">
      {TABS.map((t) => (
        <button
          key={t.id}
          role="tab"
          aria-selected={active === t.id}
          className={`nav-tab${active === t.id ? " active" : ""}`}
          onClick={() => onChange(t.id)}
        >
          {t.label}
        </button>
      ))}
    </nav>
  );
}
