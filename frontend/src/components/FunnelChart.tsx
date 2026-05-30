import React from "react";

export type FunnelData = {
  entry_count: number;
  zone_visit_count: number;
  billing_queue_count: number;
  purchase_count: number;
  entry_to_zone_dropoff_pct?: number;
  zone_to_billing_dropoff_pct?: number;
  billing_to_purchase_dropoff_pct?: number;
};

type Stage = { label: string; value: number; dropoff?: number };

export const FunnelChart: React.FC<{ data: FunnelData | null }> = ({ data }) => {
  if (!data) return <div className="text-zinc-500 text-sm">No funnel data yet.</div>;

  const stages: Stage[] = [
    { label: "1. Entry",         value: data.entry_count },
    { label: "2. Zone Visit",    value: data.zone_visit_count,    dropoff: data.entry_to_zone_dropoff_pct },
    { label: "3. Billing Queue", value: data.billing_queue_count, dropoff: data.zone_to_billing_dropoff_pct },
    { label: "4. Purchase",      value: data.purchase_count,      dropoff: data.billing_to_purchase_dropoff_pct },
  ];
  const max = Math.max(1, ...stages.map((s) => s.value));

  return (
    <div className="space-y-2">
      {stages.map((s) => {
        const pct = (s.value / max) * 100;
        const tone =
          s.dropoff != null && s.dropoff > 60 ? "bg-crit/70"
          : s.dropoff != null && s.dropoff > 30 ? "bg-warn/70"
          : "bg-accent/70";
        return (
          <div key={s.label} className="flex items-center gap-3">
            <div className="w-32 shrink-0 text-sm text-zinc-300">{s.label}</div>
            <div className="flex-1 h-7 bg-panel2 rounded relative overflow-hidden">
              <div
                className={`h-full ${tone} transition-all duration-500`}
                style={{ width: `${pct}%` }}
              />
              <div className="absolute inset-0 flex items-center px-3 text-sm font-medium text-zinc-100">
                {s.value}
              </div>
            </div>
            <div className="w-20 text-right text-xs text-zinc-400 tabular-nums">
              {s.dropoff != null ? `−${s.dropoff.toFixed(1)}%` : "—"}
            </div>
          </div>
        );
      })}
    </div>
  );
};

export default FunnelChart;
