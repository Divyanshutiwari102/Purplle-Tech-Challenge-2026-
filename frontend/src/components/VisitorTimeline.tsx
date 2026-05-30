import React, { useMemo } from "react";
import type { TickEvent } from "./LiveEventTicker";

export const VisitorTimeline: React.FC<{ events: TickEvent[] }> = ({ events }) => {
  // Group events by visitor_id, keep last 6 visitors.
  const grouped = useMemo(() => {
    const m = new Map<string, TickEvent[]>();
    for (const e of events) {
      if (!m.has(e.visitor_id)) m.set(e.visitor_id, []);
      m.get(e.visitor_id)!.push(e);
    }
    return Array.from(m.entries()).slice(-6).reverse();
  }, [events]);

  if (!grouped.length)
    return <div className="text-zinc-500 text-sm">No visitors yet.</div>;

  return (
    <div className="space-y-3 max-h-72 overflow-auto pr-1 scroll-hidden">
      {grouped.map(([vid, evs]) => (
        <div key={vid} className="bg-panel2 rounded-lg p-3 border border-line">
          <div className="text-sm font-medium text-zinc-100 mb-2">{vid}</div>
          <div className="flex flex-wrap gap-1">
            {evs.map((e, i) => (
              <span
                key={i}
                className="text-[10px] uppercase tracking-wider rounded bg-panel border border-line px-2 py-0.5"
                title={`${e.ts}${e.zone_id ? " @" + e.zone_id : ""}`}
              >
                {e.event_type.replace("BILLING_QUEUE_", "BILL_")}
              </span>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
};

export default VisitorTimeline;
