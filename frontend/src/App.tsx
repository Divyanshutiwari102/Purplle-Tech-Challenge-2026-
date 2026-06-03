import React, { useState } from "react";
import { useDashboard } from "./hooks/useApi";
import { jpost } from "./api";
import MetricCard from "./components/MetricCard";
import FunnelChart from "./components/FunnelChart";
import HeatmapChart, { Zone } from "./components/HeatmapChart";
import AnomaliesLog from "./components/AnomaliesLog";
import LiveEventTicker from "./components/LiveEventTicker";
import CameraFeed from "./components/CameraFeed";
import VisitorTimeline from "./components/VisitorTimeline";
import QueueTelemetry from "./components/QueueTelemetry";

const STORES = ["STORE_BLR_002", "ST1008"];

const App: React.FC = () => {
  const [storeId, setStoreId] = useState(STORES[0]);
  const dash = useDashboard(storeId);
  const [simRunning, setSimRunning] = useState(false);
  const [simSpeed, setSimSpeed] = useState(1);

  const startSim = async (speed: number) => {
    await jpost(`/simulation/start?speed=${speed}`);
    setSimRunning(true);
    setSimSpeed(speed);
  };
  const stopSim = async () => {
    await jpost("/simulation/stop");
    setSimRunning(false);
  };
  const setSpeed = async (speed: number) => {
    await jpost(`/simulation/speed?speed=${speed}`);
    setSimSpeed(speed);
  };

  const m = dash.metrics;
  const stale = dash.health?.stale_feeds ?? [];
  const status = dash.health?.status ?? "unknown";

  return (
    <div className="min-h-screen p-4 md:p-6 max-w-[1400px] mx-auto">
      {/* header */}
      <header className="flex flex-wrap items-center justify-between gap-3 mb-4">
        <div>
          <h1 className="text-2xl font-bold">🛒 Store Intelligence — live</h1>
          <p className="text-zinc-500 text-sm">
            Real-time analytics for offline retail. Polls every 3 s plus SSE event stream.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span
            className={`inline-block w-2 h-2 rounded-full ${
              status === "ok" ? "bg-accent" : status === "warning" ? "bg-warn" : "bg-crit"
            }`}
          />
          <span className="text-sm text-zinc-300 capitalize">{status}</span>
          <span className="text-xs text-zinc-500">
            • uptime {dash.health?.uptime_seconds ?? 0}s
          </span>
          <select
            className="ml-3 bg-panel border border-line text-zinc-100 rounded px-2 py-1 text-sm"
            value={storeId}
            onChange={(e) => setStoreId(e.target.value)}
          >
            {STORES.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
        </div>
      </header>

      {dash.error && (
        <div className="mb-3 rounded border border-crit bg-crit/10 text-crit text-sm p-2">
          API error: {dash.error}
        </div>
      )}

      {stale.length > 0 && (
        <div className="mb-3 rounded border border-warn bg-warn/10 text-warn text-sm p-2">
          STALE_FEED: {stale.join(", ")} — no events in the last 10 minutes.
        </div>
      )}

      {/* simulation controls */}
      <div className="bg-panel rounded-xl border border-line p-3 mb-4 flex flex-wrap items-center gap-2">
        <span className="text-sm text-zinc-300">Simulation:</span>
        {!simRunning ? (
          <>
            <button onClick={() => startSim(1)} className="px-3 py-1 rounded bg-accent text-black text-sm">
              ▶ Start 1×
            </button>
            <button onClick={() => startSim(2)} className="px-3 py-1 rounded bg-accent/80 text-black text-sm">
              ▶▶ 2×
            </button>
            <button onClick={() => startSim(5)} className="px-3 py-1 rounded bg-accent/60 text-black text-sm">
              ▶▶▶ 5×
            </button>
          </>
        ) : (
          <>
            <span className="text-xs text-zinc-400">running @ {simSpeed}×</span>
            <button onClick={() => setSpeed(1)} className="px-2 py-1 rounded bg-panel2 border border-line text-xs">1×</button>
            <button onClick={() => setSpeed(2)} className="px-2 py-1 rounded bg-panel2 border border-line text-xs">2×</button>
            <button onClick={() => setSpeed(5)} className="px-2 py-1 rounded bg-panel2 border border-line text-xs">5×</button>
            <button onClick={stopSim} className="px-3 py-1 rounded bg-crit/80 text-white text-sm">■ Stop</button>
          </>
        )}
      </div>

      {/* KPI cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
        <MetricCard label="Unique visitors today" value={m?.unique_visitors ?? 0} tone="info" />
        <MetricCard
          label="Conversion rate"
          value={`${((m?.conversion_rate ?? 0) * 100).toFixed(1)}%`}
          tone={m && m.conversion_rate > 0.2 ? "ok" : "warn"}
        />
        <MetricCard label="Queue depth (now)" value={m?.current_queue_depth ?? 0}
          tone={(m?.current_queue_depth ?? 0) > 5 ? "crit" : "ok"} />
        <MetricCard
          label="Abandonment"
          value={`${((m?.abandonment_rate ?? 0) * 100).toFixed(1)}%`}
          tone={(m?.abandonment_rate ?? 0) > 0.3 ? "warn" : "ok"}
        />
      </div>

      {/* main grid */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 space-y-4">
          <CameraFeed cameras={dash.cameras} storeId={storeId} />
          <div className="bg-panel rounded-xl border border-line p-4">
            <h2 className="text-sm font-medium mb-3">Conversion funnel</h2>
            <FunnelChart data={dash.funnel} />
          </div>
          <div className="bg-panel rounded-xl border border-line p-4">
            <h2 className="text-sm font-medium mb-3">Zone heatmap</h2>
            <HeatmapChart zones={(dash.heatmap?.zones as Zone[]) ?? []} />
          </div>
        </div>
        <div className="space-y-4">
          <QueueTelemetry points={dash.queueSeries} />
          <div className="bg-panel rounded-xl border border-line p-4">
            <h2 className="text-sm font-medium mb-3">Active anomalies</h2>
            <AnomaliesLog items={dash.anomalies?.anomalies ?? []} />
          </div>
          <div className="bg-panel rounded-xl border border-line p-4">
            <h2 className="text-sm font-medium mb-3">Live event ticker (SSE)</h2>
            <LiveEventTicker events={dash.events} />
          </div>
          <div className="bg-panel rounded-xl border border-line p-4">
            <h2 className="text-sm font-medium mb-3">Visitor timelines</h2>
            <VisitorTimeline events={dash.events} />
          </div>
        </div>
      </div>

      <footer className="mt-6 text-xs text-zinc-500 flex flex-wrap gap-3">
        <span>API: <code>{dash.apiBase}</code></span>
        <span>Store: <code>{storeId}</code></span>
        <span>Cameras: {dash.cameras.join(", ") || "—"}</span>
      </footer>
    </div>
  );
};

export default App;
