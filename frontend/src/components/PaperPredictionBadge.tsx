interface PaperPredictionBadgeProps {
  /** True when the decision came from the Jev MOCK path — labeled MOCK. */
  modelMock?: boolean | null;
  /** True when the row is synthetic SAMPLE data — labeled SAMPLE. */
  sample?: boolean;
}

/**
 * The literal honesty badge every prediction surface must carry.
 * Paper-only by construction: no order was placed, no wallet was touched.
 */
export default function PaperPredictionBadge({
  modelMock,
  sample,
}: PaperPredictionBadgeProps) {
  return (
    <span
      className="paper-pred-badge"
      title="Paper prediction only — no order was placed and no wallet was touched"
    >
      paper prediction · no trade placed
      {modelMock && <span className="chip chip-abstain badge-chip">MOCK</span>}
      {sample && <span className="chip chip-abstain badge-chip">SAMPLE</span>}
    </span>
  );
}
