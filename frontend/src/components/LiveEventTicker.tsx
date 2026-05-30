import React from "react";

export type TickEvent = {
  ts: string;
  event_type: string;
  visitor_id: string;
  camera_id?: string;
  zone_id?: string | null;
};

const COLOUR: Record<string, string> = {
  ENTRY: "text-accent",
  EXIT: "text-warn",
  REENTRY: "text-info",
  ZONE_ENTER: "text-info",
  ZONE_DWELL: "text-zinc-300",
  ZONE_EXIT: "text-zinc-400",
  BILLING_QUEUE_JOIN: "text-warn",
  BILLING_QUEUE_ABANDON: "text-crit",
};

export const LiveEventTicker: React.FC<{ events: TickEvent[] }> = ({ events }) => {
  if (!events.length)
    return <div className="text-zinc-500 text-sm">Waiting for events…</div>;
  return (
    <div className="font-mono text-xs space-y-1 max-h-72 overflow-auto pr-1 scroll-hidden">
      {events.slice().reverse().map((e, i) => (
        <div key={i} className="flex gap-2">
          <span className="text-zinc-500">{e.ts.slice(11, 19)}</span>
          <span className={COLOUR[e.event_type] ?? "text-zinc-300"}>
            {e.event_type}
          </span>
          <span className="text-zinc-400">{e.visitor_id}</span>
          {e.zone_id && <span className="text-zinc-500">@{e.zone_id}</span>}
          {e.camera_id && <span className="text-zinc-600">[{e.camera_id}]</span>}
        </div>
      ))}
    </div>
  );
};

export default LiveEventTicker;
