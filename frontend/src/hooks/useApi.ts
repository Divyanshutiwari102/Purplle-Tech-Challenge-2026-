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

  // /cameras is essentially static (clip + detection metadata), so it
  // doesn't need to be polled. Fetch it once per store change. Keeping
  // it out of the periodic loop frees one of the browser's six
  // per-host connections — important when SSE + MJPEG are also open.
  useEffect(() => {
    let alive = true;
    jget<CameraInfo>(`/cameras?store_id=${encodeURIComponent(storeId)}`)
      .then((c) => alive && setCameraInfo(c))
      .catch(() => {
        // Retry once after 2s — if the API was slow during a sim
        // start, the next attempt usually succeeds.
        setTimeout(() => {
          if (!alive) return;
          jget<CameraInfo>(`/cameras?store_id=${encodeURIComponent(storeId)}`)
            .then((c) => alive && setCameraInfo(c))
            .catch(() => {});
        }, 2000);
      });
    return () => { alive = false; };
  }, [storeId]);

  // Polling refresh of summary endpoints.
  //
  // First fetch after a store change runs all endpoints in parallel so
  // the dashboard repaints fast (the switch feels instant). Steady-state
  // ticks then run sequentially so we never hold > 1 fetch connection at
  // once, leaving the browser's per-host pool free for SSE + MJPEG.
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;

    const firstPaint = async () => {
      try {
        const [m, f, h, a, hl] = await Promise.all([
          jget<Metrics>(`/stores/${storeId}/metrics`),
          jget<Funnel>(`/stores/${storeId}/funnel`),
          jget<Heatmap>(`/stores/${storeId}/heatmap`),
          jget<Anomalies>(`/stores/${storeId}/anomalies`),
          jget<Health>(`/health`),
        ]);
        if (!alive) return;
        setMetrics(m);
        setFunnel(f);
        setHeatmap(h);
        setAnomalies(a);
        setHealth(hl);
        setQueueSeries((prev) =>
          [...prev, { ts: Date.now(), depth: m.current_queue_depth }].slice(-120)
        );
        setError(null);
      } catch (e: any) {
        if (alive) setError(e?.message ?? "request failed");
      } finally {
        if (alive) timer = setTimeout(tick, 5000);
      }
    };

    const tick = async () => {
      try {
        const m = await jget<Metrics>(`/stores/${storeId}/metrics`);
        if (!alive) return;
        setMetrics(m);
        setQueueSeries((prev) =>
          [...prev, { ts: Date.now(), depth: m.current_queue_depth }].slice(-120)
        );

        const f = await jget<Funnel>(`/stores/${storeId}/funnel`);
        if (!alive) return;
        setFunnel(f);

        const h = await jget<Heatmap>(`/stores/${storeId}/heatmap`);
        if (!alive) return;
        setHeatmap(h);

        const a = await jget<Anomalies>(`/stores/${storeId}/anomalies`);
        if (!alive) return;
        setAnomalies(a);

        const hl = await jget<Health>(`/health`);
        if (!alive) return;
        setHealth(hl);

        setError(null);
      } catch (e: any) {
        if (alive) setError(e?.message ?? "request failed");
      } finally {
        if (alive) timer = setTimeout(tick, 5000);
      }
    };

    firstPaint();
    return () => {
      alive = false;
      clearTimeout(timer!);
    };
  }, [storeId]);

  // SSE for the live event ticker. Reconnects automatically on store
  // change (old connection closed in cleanup) and on transport error.
  useEffect(() => {
    let es: EventSource | null = null;
    let closed = false;
    let retry: ReturnType<typeof setTimeout>;

    const connect = () => {
      if (closed) return;
      try {
        es = new EventSource(sseUrl(storeId));
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
            /* ignore parse errors (e.g. heartbeats) */
          }
        };
        es.onerror = () => {
          // Browser usually auto-reconnects, but if the connection is
          // fully closed, force a fresh one after a short backoff.
          if (es && es.readyState === EventSource.CLOSED && !closed) {
            es.close();
            clearTimeout(retry);
            retry = setTimeout(connect, 2000);
          }
        };
      } catch {
        // SSE unsupported / blocked — polling still keeps the UI live.
      }
    };

    connect();
    return () => {
      closed = true;
      clearTimeout(retry);
      if (es) es.close();
    };
  }, [storeId]);

  return { metrics, funnel, heatmap, anomalies, health, events, queueSeries, cameraInfo, error, apiBase };
}
