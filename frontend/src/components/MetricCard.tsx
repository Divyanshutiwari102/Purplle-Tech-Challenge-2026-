import React from "react";

type Props = {
  label: string;
  value: string | number;
  hint?: string;
  tone?: "ok" | "warn" | "crit" | "info";
};

const TONE: Record<NonNullable<Props["tone"]>, string> = {
  ok:   "text-accent",
  warn: "text-warn",
  crit: "text-crit",
  info: "text-info",
};

export const MetricCard: React.FC<Props> = ({ label, value, hint, tone = "info" }) => (
  <div className="bg-panel rounded-xl border border-line p-4 flex flex-col gap-1 shadow-sm">
    <div className="text-xs uppercase tracking-wider text-zinc-400">{label}</div>
    <div className={`text-3xl font-bold tabular-nums ${TONE[tone]}`}>{value}</div>
    {hint && <div className="text-xs text-zinc-500">{hint}</div>}
  </div>
);

export default MetricCard;
