import React from "react";

export type Zone = {
  zone_id: string;
  visit_frequency: number;
  avg_dwell_ms: number;
  normalized_score: number;
  data_confidence: boolean;
  session_count?: number;
};

const colourFor = (score: number) => {
  // 0 → cool, 100 → warm. We blend two HSL anchors.
  const t = Math.max(0, Math.min(100, score)) / 100;
  const h = 200 - t * 200; // 200 (blue) → 0 (red)
  const s = 70;
  const l = 30 + t * 30;   // 30 → 60
  return `hsl(${h.toFixed(0)} ${s}% ${l}%)`;
};

export const HeatmapChart: React.FC<{ zones: Zone[] }> = ({ zones }) => {
  if (!zones.length)
    return <div className="text-zinc-500 text-sm">No zone activity yet.</div>;

  return (
    <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
      {zones.map((z) => (
        <div
          key={z.zone_id}
          className="rounded-lg p-3 border border-line"
          style={{ background: colourFor(z.normalized_score) }}
        >
          <div className="text-xs uppercase tracking-wider opacity-80">
            {z.zone_id}
          </div>
          <div className="text-2xl font-bold tabular-nums">
            {z.normalized_score.toFixed(0)}
          </div>
          <div className="text-xs opacity-80">
            visits {z.visit_frequency} • dwell {(z.avg_dwell_ms / 1000).toFixed(1)}s
          </div>
          {!z.data_confidence && (
            <div className="text-[10px] uppercase mt-1 bg-black/40 rounded px-1 inline-block">
              low confidence
            </div>
          )}
        </div>
      ))}
    </div>
  );
};

export default HeatmapChart;
