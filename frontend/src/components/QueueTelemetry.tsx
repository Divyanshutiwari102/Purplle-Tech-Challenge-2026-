import React from "react";

export type QueuePoint = { ts: number; depth: number };

export const QueueTelemetry: React.FC<{ points: QueuePoint[] }> = ({ points }) => {
  const w = 320;
  const h = 100;
  const data = points.slice(-60); // last 60 samples
  const max = Math.max(5, ...data.map((p) => p.depth));
  const stepX = data.length > 1 ? w / (data.length - 1) : w;

  const pathD = data
    .map((p, i) => {
      const x = i * stepX;
      const y = h - (p.depth / max) * (h - 8) - 4;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  const last = data[data.length - 1]?.depth ?? 0;
  const tone =
    last > 5 ? "text-crit" : last > 2 ? "text-warn" : "text-accent";

  return (
    <div className="bg-panel rounded-xl border border-line p-4">
      <div className="flex items-baseline justify-between mb-2">
        <div className="text-xs uppercase tracking-wider text-zinc-400">Queue depth</div>
        <div className={`text-2xl font-bold tabular-nums ${tone}`}>{last}</div>
      </div>
      <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-24" preserveAspectRatio="none">
        <line x1="0" y1={h - 4} x2={w} y2={h - 4} stroke="#262b39" strokeWidth="1" />
        <path d={pathD} fill="none" stroke="currentColor" strokeWidth="2"
              className={tone} />
      </svg>
      <div className="text-xs text-zinc-500 mt-1">
        rolling 60-sample window • critical at 6+
      </div>
    </div>
  );
};

export default QueueTelemetry;
