import React, { useEffect, useState } from "react";
import { cameraSrc, posterSrc } from "../api";

type CamMode = {
  mode: "real" | "sim";
  role?: string;
  clip?: string;
  fps?: number;
  n_detected_frames?: number;
  frame_w?: number;
  frame_h?: number;
};

export type CameraInfo = {
  store_id?: string;
  stores?: string[];
  cameras: string[];
  modes: Record<string, CamMode>;
};

type CamMeta = { label: string; role: string; overlays: string[] };

// Per-store camera metadata. Cameras the layout doesn't know about
// fall back to a generic FLOOR card so the UI never blanks out.
const META_BY_STORE: Record<string, Record<string, CamMeta>> = {
  STORE_BLR_002: {
    CAM_1: { label: "Main Entrance",   role: "ENTRY",   overlays: ["Entry Threshold", "Entry Crossing Line"] },
    CAM_2: { label: "Main Floor",      role: "FLOOR",   overlays: ["Skincare Aisles", "Moisturiser", "Fragrances", "Makeup"] },
    CAM_3: { label: "Secondary Floor", role: "FLOOR",   overlays: ["Haircare", "Bodycare"] },
    CAM_4: { label: "Billing Counter", role: "BILLING", overlays: ["Billing Counter"] },
    CAM_5: { label: "Billing Queue",   role: "BILLING", overlays: ["Billing Queue"] },
  },
  ST1008: {
    ENTRY_1:      { label: "Entry Door 1", role: "ENTRY",   overlays: ["Entry Crossing Line"] },
    ENTRY_2:      { label: "Entry Door 2", role: "ENTRY",   overlays: ["Entry Crossing Line"] },
    ZONE:         { label: "Main Floor",   role: "FLOOR",   overlays: ["Skincare", "Fragrance", "Makeup", "Haircare"] },
    BILLING_AREA: { label: "Billing Area", role: "BILLING", overlays: ["Billing Area"] },
  },
};

const _DEFAULT_META: CamMeta = { label: "Camera", role: "FLOOR", overlays: [] };
const _metaFor = (storeId: string, cam: string): CamMeta =>
  META_BY_STORE[storeId]?.[cam] ?? _DEFAULT_META;

type Props = {
  cameraInfo: CameraInfo | null;
  storeId: string;
};

export const CameraFeed: React.FC<Props> = ({ cameraInfo, storeId }) => {
  // Source of truth: the parent's polled `/cameras?store_id=...` response.
  // Falls back to the metadata table only while the first poll is in flight,
  // so the thumbnail strip is never empty.
  const polledList = cameraInfo?.cameras ?? [];
  const fallbackList = Object.keys(META_BY_STORE[storeId] ?? {});
  const list = polledList.length ? polledList : fallbackList;

  const [active, setActive] = useState<string>(list[0] ?? "");
  const [bumpKey, setBumpKey] = useState(0);   // forces <img> reload on store/speed change
  const [speed, setSpeed] = useState(1);
  const [streamLoaded, setStreamLoaded] = useState(false);  // MJPEG first frame arrived?

  // Whenever the store changes (or the camera list changes shape),
  // reset the active camera to the new store's first one and bump
  // the <img> key so the MJPEG socket reconnects to the new store.
  useEffect(() => {
    if (list.length && !list.includes(active)) {
      setActive(list[0]);
    }
    setBumpKey((k) => k + 1);
    setStreamLoaded(false);
  }, [storeId, list.join("|")]);   // eslint-disable-line react-hooks/exhaustive-deps

  // Reset the loaded flag whenever the active camera or speed changes so
  // the poster shows again until the new MJPEG delivers its first frame.
  useEffect(() => {
    setStreamLoaded(false);
  }, [active, speed]);

  const camMode = cameraInfo?.modes?.[active];
  // While the first /cameras response is in flight, show a "loading" badge
  // instead of the misleading "synthetic" label.
  const loading = !cameraInfo;
  const mode: "real" | "sim" | "loading" = loading
    ? "loading"
    : camMode?.mode ?? "sim";
  const clip = camMode?.clip;
  const nativeFps = camMode?.fps ?? 30;
  const meta = _metaFor(storeId, active);

  const fpsParam = speed === 1 ? 0 : nativeFps * speed;
  const baseSrc = active ? cameraSrc(active, storeId) : "";
  const imgSrc = !active
    ? ""
    : `${baseSrc}${fpsParam ? (baseSrc.includes("?") ? `&fps=${fpsParam}` : `?fps=${fpsParam}`) : ""}${
        baseSrc.includes("?") ? `&_k=${bumpKey}` : `?_k=${bumpKey}`
      }`;
  // Poster is a single cacheable JPEG — used as the always-visible base
  // layer behind the MJPEG. If the live stream stalls (browser per-host
  // socket limits, proxy hiccup) the poster keeps a real frame on screen
  // instead of a black box.
  const poster = active ? posterSrc(active, storeId) : "";

  // Size the video container to match the clip's actual aspect ratio so a
  // portrait clip (e.g. ST1008's billing_area, 960×1080) doesn't render as
  // a tiny strip inside a 16:9 letterbox.
  const fw = camMode?.frame_w ?? 1920;
  const fh = camMode?.frame_h ?? 1080;
  const aspectStyle = { aspectRatio: `${fw} / ${fh}` };

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
            <span className="text-xs text-zinc-500">
              ({active || "…"} • {storeId})
            </span>
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
        <div
          className="relative rounded-lg overflow-hidden border border-line bg-black mx-auto w-full max-w-full"
          style={{ ...aspectStyle, maxHeight: "60vh" }}
        >
          {/* Mode badge */}
          <div className="absolute top-2 left-2 z-10 flex items-center gap-2 bg-black/70 px-2 py-1 rounded">
            <span
              className={`w-2 h-2 rounded-full ${
                mode === "real"
                  ? "bg-accent animate-pulse"
                  : mode === "sim"
                  ? "bg-warn"
                  : "bg-zinc-400 animate-pulse"
              }`}
            />
            <span className="text-xs uppercase tracking-wider">
              {mode === "real"
                ? "● Live (real CCTV)"
                : mode === "sim"
                ? "● Live (synthetic)"
                : "● Connecting…"}
            </span>
          </div>
          {imgSrc ? (
            <>
              {/* Always-visible base layer: a real poster frame so the
                  panel is never black even if the MJPEG socket stalls. */}
              {poster && (
                <img
                  src={poster}
                  alt={`${active} poster`}
                  className="absolute inset-0 w-full h-full object-contain"
                />
              )}
              {/* Live MJPEG layered on top; fades in once its first
                  frame loads. */}
              <img
                key={`${storeId}-${active}-${bumpKey}`}
                src={imgSrc}
                alt={`${active} ${meta.label}`}
                onLoad={() => setStreamLoaded(true)}
                onError={() => setStreamLoaded(false)}
                className={`absolute inset-0 w-full h-full object-contain transition-opacity duration-300 ${
                  streamLoaded ? "opacity-100" : "opacity-0"
                }`}
              />
            </>
          ) : (
            <div className="w-full h-full flex items-center justify-center text-zinc-500 text-xs">
              No cameras for {storeId}
            </div>
          )}
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
              <KPI
                label="Mode"
                value={mode === "real" ? "REAL" : mode === "sim" ? "SIM" : "…"}
                tone={mode === "real" ? "ok" : mode === "sim" ? "warn" : "info"}
              />
              <KPI label="Speed" value={speed === 1 ? "1×" : `${speed}×`} tone="info" />
              <KPI label="Source" value={mode === "real" ? "MP4" : mode === "sim" ? "Drawn" : "—"} tone="info" />
              <KPI label="Frames" value={camMode?.n_detected_frames?.toString() ?? "—"} tone="info" />
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
      <div className={`mt-4 grid grid-cols-2 gap-2 ${list.length >= 5 ? "sm:grid-cols-5" : "sm:grid-cols-4"}`}>
        {list.map((c) => {
          const m = _metaFor(storeId, c);
          const cm = cameraInfo?.modes?.[c]?.mode;
          return (
            <button
              key={`${storeId}-${c}`}
              onClick={() => setActive(c)}
              className={`text-left rounded-lg border overflow-hidden transition ${
                active === c
                  ? "bg-panel2 border-accent"
                  : "bg-panel2/60 border-line hover:border-accent/40"
              }`}
            >
              {/* Static poster — single JPEG, cacheable, doesn't
                  hold an MJPEG socket. Unlike <img src={mjpeg}>
                  which would cost one of the browser's six
                  per-host connections per thumbnail. */}
              <div className="aspect-video bg-black">
                <img
                  src={posterSrc(c, storeId)}
                  alt={`${c} preview`}
                  loading="lazy"
                  className="w-full h-full object-cover"
                  onError={(e) => {
                    (e.currentTarget as HTMLImageElement).style.opacity = "0.2";
                  }}
                />
              </div>
              <div className="p-2">
                <div className="text-[10px] uppercase tracking-wider text-zinc-500">
                  {m.role}
                </div>
                <div className="text-sm font-medium truncate">{m.label}</div>
                <div className="flex items-center gap-1 mt-1">
                  <span
                    className={`w-1.5 h-1.5 rounded-full ${
                      cm === "real" ? "bg-accent" : cm === "sim" ? "bg-warn" : "bg-zinc-500"
                    }`}
                  />
                  <span className="text-[10px] text-zinc-500">{c}</span>
                </div>
              </div>
            </button>
          );
        })}
      </div>

      <div className="mt-2 text-[11px] text-zinc-500">
        {mode === "real"
          ? "Real CCTV frames with YOLO bounding boxes overlaid. Detections were pre-computed once via pipeline.precompute_detections; the boxes you see are the actual model output replayed in sync with the clip. Footage is challenge-licensed and never leaves the local container."
          : mode === "sim"
          ? "Synthetic frames with bounding boxes, zone polygons and HUD. The repo runs in this mode by default; drop the licensed clips into data/clips/ and the matching detections into data/detections/ to switch to real CCTV."
          : "Waiting for /cameras response — telemetry will populate once the API replies."}
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
