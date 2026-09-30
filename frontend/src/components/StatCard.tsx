interface StatCardProps {
  label: string;
  value: string;
  /** Optional color class: "green" | "red" | "amber" | default */
  tone?: "green" | "red" | "amber";
}

export default function StatCard({ label, value, tone }: StatCardProps) {
  return (
    <div className="stat-card">
      <div className="stat-label">{label}</div>
      <div className={`stat-value${tone ? ` tone-${tone}` : ""}`}>{value}</div>
    </div>
  );
}
