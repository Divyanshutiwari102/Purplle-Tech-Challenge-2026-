import { useEffect, useRef, useState } from "react";
import { jget, sseUrl, apiBase } from "../api";
import type { TickEvent } from "../components/LiveEventTicker";

type Metrics = {
  store_id: string;
  unique_visitors: number;
  conversion_rate: number;
  current_queue_depth: number;
  abandonment_rate: number;
  avg_dwell_per_zone: Record<string, number>;
};

type Funnel = {
  entry_count: number;
  zone_visit_count: number;
  billing_queue_count: number;
  purchase_count: number;
  entry_to_zone_dropoff_pct: number;
  zone_to_billing_dropoff_pct: number;
  billing_to_purchase_dropoff_pct: number;
};

type Heatmap = { zones: Array<any> };
type Anomalies = { anomalies: Array<any> };
type Health = { status: string; uptime_seconds: number; stale_feeds: string[]; last_event_per_store: Record<string, string> };
type CamMode = { mode: "real" | "sim"; clip?: string; fps?: number; n_detected_frames?: number };
type CameraInfo = {
  store_id?: string;
  stores?: string[];
  cameras: string[];
  modes: Record<string, CamMode>;
};

export function useDashboard(storeId: string) {
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [funnel, setFunnel] = useState<Funnel | null>(null);
  const [heatmap, setHeatmap] = useState<Heatmap | null>(null);
  const [anomalies, setAnomalies] = useState<Anomalies | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [events, setEvents] = useState<TickEvent[]>([]);
  const [queueSeries, setQueueSeries] = useState<Array<{ ts: number; depth: number }>>([]);
  const [cameraInfo, setCameraInfo] = useState<CameraInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const sseRef = useRef<EventSource | null>(null);

  // When the active store changes, immediately drop cached camera data
  // so the CameraFeed component re-renders with the new store's data
  // on the next tick instead of briefly flashing the previous store's
  // thumbnail strip.
  useEffect(() => {
    setCameraInfo(null);
    setEvents([]);
    setQueueSeries([]);
  }, [storeId]);

  // Polling refresh of summary endpoints (cheap, 3s).
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try {
        const [m, f, h, a, hl, c] = await Promise.all([
          jget<Metrics>(`/stores/${storeId}/metrics`),
          jget<Funnel>(`/stores/${storeId}/funnel`),
          jget<Heatmap>(`/stores/${storeId}/heatmap`),
          jget<Anomalies>(`/stores/${storeId}/anomalies`),
          jget<Health>(`/health`),
          jget<CameraInfo>(`/cameras?store_id=${encodeURIComponent(storeId)}&_t=${Date.now()}`),
        ]);
        if (!alive) return;
        setMetrics(m);
        setFunnel(f);
        setHeatmap(h);
        setAnomalies(a);
        setHealth(hl);
        setCameraInfo(c);
        setQueueSeries((prev) =>
          [...prev, { ts: Date.now(), depth: m.current_queue_depth }].slice(-120)
        );
        setError(null);
      } catch (e: any) {
        setError(e?.message ?? "request failed");
      } finally {
        if (alive) timer = setTimeout(tick, 3000);
      }
    };
    tick();
    return () => {
      alive = false;
      clearTimeout(timer!);
    };
  }, [storeId]);

  // SSE for the live event ticker.
  useEffect(() => {
    try {
      const es = new EventSource(sseUrl(storeId));
      sseRef.current = es;
      es.onmessage = (m) => {
        try {
          const obj = JSON.parse(m.data);
          if (obj.type === "sim_event") {
            setEvents((prev) =>
              [
                ...prev,
                {
                  ts: obj.ts,
                  event_type: obj.event_type,
                  visitor_id: obj.visitor_id,
                  camera_id: obj.camera_id,
                  zone_id: obj.zone_id,
                },
              ].slice(-200)
            );
          }
        } catch {
          /* ignore parse errors */
        }
      };
      es.onerror = () => {
        // EventSource auto-reconnects.
      };
      return () => es.close();
    } catch {
      // SSE not supported / blocked. Polling still keeps the UI live.
    }
  }, [storeId]);

  return { metrics, funnel, heatmap, anomalies, health, events, queueSeries, cameraInfo, error, apiBase };
}
