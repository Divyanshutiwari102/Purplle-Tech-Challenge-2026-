import React from "react";

export type Anomaly = {
  type: string;
  severity: "INFO" | "WARN" | "CRITICAL";
  description: string;
  suggested_action: string;
};

const TONE: Record<Anomaly["severity"], { border: string; bg: string; label: string }> = {
  INFO:     { border: "border-info",  bg: "bg-info/10",  label: "text-info" },
  WARN:     { border: "border-warn",  bg: "bg-warn/10",  label: "text-warn" },
  CRITICAL: { border: "border-crit",  bg: "bg-crit/10",  label: "text-crit" },
};

export const AnomaliesLog: React.FC<{ items: Anomaly[] }> = ({ items }) => {
  if (!items.length)
    return <div className="text-accent text-sm">No active anomalies.</div>;
  return (
    <div className="space-y-2 max-h-72 overflow-auto pr-1 scroll-hidden">
      {items.map((a, i) => (
        <div key={i} className={`rounded-lg border ${TONE[a.severity].border} ${TONE[a.severity].bg} p-3`}>
          <div className="flex justify-between items-center mb-1">
            <span className={`text-xs font-bold ${TONE[a.severity].label}`}>
              {a.severity}
            </span>
            <span className="text-xs text-zinc-400">{a.type}</span>
          </div>
          <div className="text-sm text-zinc-100">{a.description}</div>
          <div className="text-xs text-zinc-400 mt-1">→ {a.suggested_action}</div>
        </div>
      ))}
    </div>
  );
};

export default AnomaliesLog;
