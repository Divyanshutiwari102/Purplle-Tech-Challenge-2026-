import React, { useEffect, useState } from "react";
import { cameraSrc, jget } from "../api";

type CamMode = { mode: "real" | "sim"; clip?: string; fps?: number; n_detected_frames?: number };

type CameraInfo = {
  cameras: string[];
  modes: Record<string, CamMode>;
};

const ROLE_BY_CAM: Record<string, { label: string; role: string; overlays: string[] }> = {
  CAM_1: { label: "Main Entrance",   role: "ENTRY",   overlays: ["Entry Threshold", "Entry Crossing Line"] },
  CAM_2: { label: "Main Floor",      role: "FLOOR",   overlays: ["Skincare Aisles", "Moisturiser", "Fragrances", "Makeup"] },
  CAM_3: { label: "Secondary Floor", role: "FLOOR",   overlays: ["Haircare", "Bodycare"] },
  CAM_4: { label: "Billing Counter", role: "BILLING", overlays: ["Billing Counter"] },
  CAM_5: { label: "Billing Queue",   role: "BILLING", overlays: ["Billing Queue"] },
};

export const CameraFeed: React.FC<{ cameras: string[] }> = ({ cameras }) => {
  const list = cameras.length ? cameras : Object.keys(ROLE_BY_CAM);
  const [active, setActive] = useState(list[0] ?? "CAM_1");
  const [info, setInfo] = useState<CameraInfo | null>(null);
  const [bumpKey, setBumpKey] = useState(0); // forces <img> re-fetch on speed change
  // speed multiplier of the clip's native fps. 0 = native (1×).
  const [speed, setSpeed] = useState(1);

  useEffect(() => {
    let alive = true;
    jget<CameraInfo>("/cameras")
      .then((d) => alive && setInfo(d))
      .catch(() => {});
    return () => { alive = false; };
  }, []);

  const mode = info?.modes?.[active]?.mode ?? "sim";
  const clip = info?.modes?.[active]?.clip;
  const nativeFps = info?.modes?.[active]?.fps ?? 30;
  const meta = ROLE_BY_CAM[active] ?? { label: active, role: "FLOOR", overlays: [] };

  // 0 sentinel means "let the server pick native fps", else cap it.
  const fpsParam = speed === 1 ? 0 : nativeFps * speed;

  return (
    <div className="bg-panel rounded-xl border border-line p-4">
      {/* Top header: title + speed presets */}
      <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
        <div>
          <div className="text-xs uppercase tracking-wider text-zinc-400">
            ▶ YOLOv8 LIVE CV STREAM
          </div>
          <div className="text-lg font-semibold">
            {meta.label}{" "}
            <span className="text-xs text-zinc-500">({active})</span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-zinc-500">Speed</span>
          {[
            { v: 0.5, label: "0.5×" },
            { v: 1,   label: "1×"   },
            { v: 2,   label: "2×"   },
            { v: 3,   label: "3×"   },
          ].map(({ v, label }) => (
            <button
              key={v}
              onClick={() => { setSpeed(v); setBumpKey((k) => k + 1); }}
              className={`px-2 py-1 rounded text-xs border ${
                speed === v
                  ? "bg-accent text-black border-accent"
                  : "bg-panel2 text-zinc-300 border-line hover:border-accent/50"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {/* Main: video + telemetry sidebar */}
      <div className="grid grid-cols-1 lg:grid-cols-[1fr_240px] gap-4">
        <div className="relative rounded-lg overflow-hidden border border-line bg-black aspect-video">
          {/* Mode badge */}
          <div className="absolute top-2 left-2 z-10 flex items-center gap-2 bg-black/70 px-2 py-1 rounded">
            <span
              className={`w-2 h-2 rounded-full ${
                mode === "real" ? "bg-accent animate-pulse" : "bg-warn"
              }`}
            />
            <span className="text-xs uppercase tracking-wider">
              {mode === "real" ? "● Live (real CCTV)" : "● Live (synthetic)"}
            </span>
          </div>
          <img
            key={`${active}-${bumpKey}`}
            src={`${cameraSrc(active)}?fps=${fpsParam}`}
            alt={`${active} ${meta.label}`}
            className="w-full h-full object-contain"
          />
        </div>

        <aside className="space-y-3">
          <div>
            <div className="text-[11px] uppercase tracking-wider text-zinc-500 mb-1">Model</div>
            <div className="space-y-1 text-sm">
              <Row k="Detector"  v="YOLOv8n" />
              <Row k="Tracker"   v="ByteTrack-IoU" />
              <Row k="Classes"   v="Person [class_id: 0]" />
              <Row k="Hardware"  v="CPU inference" />
            </div>
          </div>

          <div>
            <div className="text-[11px] uppercase tracking-wider text-zinc-500 mb-1">Telemetry</div>
            <div className="grid grid-cols-2 gap-2">
              <KPI label="Mode"  value={mode === "real" ? "REAL" : "SIM"} tone={mode === "real" ? "ok" : "warn"} />
              <KPI label="Speed" value={speed === 1 ? "1×" : `${speed}×`} tone="info" />
              <KPI label="Source" value={mode === "real" ? "MP4" : "Drawn"} tone="info" />
              <KPI label="Frames" value={info?.modes?.[active]?.n_detected_frames?.toString() ?? "—"} tone="info" />
            </div>
            <div className="mt-1 text-[11px] text-zinc-500 truncate">
              native: <code>{nativeFps.toFixed(1)} fps</code>
              {clip && <> • clip: <code>{clip}</code></>}
            </div>
          </div>

          <div>
            <div className="text-[11px] uppercase tracking-wider text-zinc-500 mb-1">
              Active overlays ({meta.overlays.length})
            </div>
            <ul className="space-y-1 text-xs">
              {meta.overlays.map((o, i) => (
                <li key={o} className="flex items-center justify-between bg-panel2 rounded px-2 py-1 border border-line">
                  <span className="flex items-center gap-2">
                    <span
                      className="w-2 h-2 rounded-full"
                      style={{
                        background: [
                          "#34d399", "#60a5fa", "#fbbf24", "#f472b6", "#a78bfa",
                        ][i % 5],
                      }}
                    />
                    {o}
                  </span>
                  <span className="text-[10px] uppercase text-zinc-500">
                    {meta.role === "ENTRY" ? "LINE" : "POLYGON"}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        </aside>
      </div>

      {/* Camera thumbnail strip */}
      <div className="mt-4 grid grid-cols-2 sm:grid-cols-5 gap-2">
        {list.map((c) => {
          const m = ROLE_BY_CAM[c] ?? { label: c, role: "FLOOR", overlays: [] };
          const cm = info?.modes?.[c]?.mode ?? "sim";
          return (
            <button
              key={c}
              onClick={() => setActive(c)}
              className={`text-left rounded-lg border p-2 transition ${
                active === c
                  ? "bg-panel2 border-accent"
                  : "bg-panel2/60 border-line hover:border-accent/40"
              }`}
            >
              <div className="text-[10px] uppercase tracking-wider text-zinc-500">
                {m.role}
              </div>
              <div className="text-sm font-medium truncate">{m.label}</div>
              <div className="flex items-center gap-1 mt-1">
                <span
                  className={`w-1.5 h-1.5 rounded-full ${
                    cm === "real" ? "bg-accent" : "bg-warn"
                  }`}
                />
                <span className="text-[10px] text-zinc-500">{c}</span>
              </div>
            </button>
          );
        })}
      </div>

      <div className="mt-2 text-[11px] text-zinc-500">
        {mode === "real"
          ? "Real CCTV frames with YOLO bounding boxes overlaid. Detections were pre-computed once via pipeline.precompute_detections; the boxes you see are the actual model output replayed in sync with the clip. Footage is challenge-licensed and never leaves the local container."
          : "Synthetic frames with bounding boxes, zone polygons and HUD. The repo runs in this mode by default; drop the licensed clips into data/clips/ and the matching detections into data/detections/ to switch to real CCTV."}
      </div>
    </div>
  );
};

const Row: React.FC<{ k: string; v: string }> = ({ k, v }) => (
  <div className="flex justify-between gap-2">
    <span className="text-zinc-500">{k}</span>
    <span className="font-mono text-zinc-200">{v}</span>
  </div>
);

const KPI: React.FC<{ label: string; value: string; tone: "ok" | "warn" | "info" }> = ({ label, value, tone }) => (
  <div className="bg-panel2 rounded border border-line px-2 py-1">
    <div className="text-[10px] uppercase tracking-wider text-zinc-500">{label}</div>
    <div
      className={`text-sm font-bold tabular-nums ${
        tone === "ok" ? "text-accent" : tone === "warn" ? "text-warn" : "text-info"
      }`}
    >
      {value}
    </div>
  </div>
);

export default CameraFeed;
