"""
Camera stream — real CCTV frames with YOLO bboxes that follow the
actual people in the clip.

Two modes, picked at request time:
  • REAL    — when both the clip mp4 and the matching pre-computed
              detection JSON are present. Decodes mp4, overlays
              bboxes / track ids / zone polygons and a HUD, and
              serves as MJPEG.
  • SYNTH   — fallback for evaluators who clone the public repo
              without our challenge-licensed clips. Same UI shape, but
              the boxes are synthetic actors moving over a generated
              floor grid.

Multi-store support
  The layout JSON now declares one entry per `store_id` under
  `stores`. For each store we resolve:
    • clip directory   : `data/clips/<_clips_subdir>` if set, else `data/clips`
    • detections dir   : `data/detections/<_detections_subdir>` if set,
                         else `data/detections`
  All store-scoped helpers below take an optional `store_id`; calls
  without one default to the first store declared in the layout
  (preserves the v1 single-store behaviour).
"""
from __future__ import annotations

import io
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter()

# ---------------------------------------------------------------------
# Layout: zone polygons per camera (also used in synthetic mode)
# ---------------------------------------------------------------------
_LAYOUT_CANDIDATES = [
    "/data/store_layout.json",
    "data/layout/store_layout.json",
    str(Path(__file__).resolve().parent.parent / "data" / "layout" / "store_layout.json"),
]


def _load_layout_full() -> dict:
    """Whole layout doc (or {} on failure)."""
    for p in _LAYOUT_CANDIDATES:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f) or {}
            except Exception:
                pass
    return {}


def _list_store_ids() -> List[str]:
    full = _load_layout_full()
    return list((full.get("stores") or {}).keys())


def _default_store_id() -> str:
    ids = _list_store_ids()
    return ids[0] if ids else "STORE_BLR_002"


def _resolve_store_id(store_id: Optional[str]) -> str:
    if store_id and store_id in _list_store_ids():
        return store_id
    return _default_store_id()


def _store_block(store_id: str) -> dict:
    full = _load_layout_full()
    return ((full.get("stores") or {}).get(store_id)) or {}


def _load_layout(store_id: Optional[str] = None) -> Dict[str, dict]:
    """Cameras dict for a given store. Empty dict if missing."""
    sid = _resolve_store_id(store_id)
    return (_store_block(sid).get("cameras") or {})


# Per-store mapping from "user-friendly id used in URLs" → "layout
# camera id". For STORE_BLR_002 we keep CAM_1..5 as the public ids
# (the React UI ships with those). For other stores we expose the
# layout ids verbatim so a store with native ids like ENTRY_1 doesn't
# get squashed into CAM_X.
_BLR_002_SHORT_TO_LAYOUT = {
    "CAM_1": "CAM_ENTRY_01",
    "CAM_2": "CAM_FLOOR_01",
    "CAM_3": "CAM_FLOOR_02",
    "CAM_4": "CAM_BILLING_01",
    "CAM_5": "CAM_BILLING_02",
}


def _short_to_layout(store_id: str) -> Dict[str, str]:
    if store_id == "STORE_BLR_002":
        return dict(_BLR_002_SHORT_TO_LAYOUT)
    # Other stores: identity mapping over whatever the layout declares.
    return {cid: cid for cid in _load_layout(store_id).keys()}


def _public_camera_ids(store_id: str) -> List[str]:
    """Camera ids the API exposes for this store, in declaration order."""
    if store_id == "STORE_BLR_002":
        return list(_BLR_002_SHORT_TO_LAYOUT.keys())
    return list(_load_layout(store_id).keys())


_DEFAULT_W, _DEFAULT_H = 960, 540

_FALLBACK_CAMERAS: Dict[str, dict] = {
    "CAM_1": {"role": "ENTRY",   "frame_size": [_DEFAULT_W, _DEFAULT_H], "entry_line_y": 270, "zones": []},
    "CAM_2": {"role": "FLOOR",   "frame_size": [_DEFAULT_W, _DEFAULT_H],
              "zones": [
                  {"zone_id": "SKINCARE",    "polygon": [[40,40],[460,40],[460,260],[40,260]]},
                  {"zone_id": "MOISTURISER", "polygon": [[480,40],[920,40],[920,260],[480,260]]},
                  {"zone_id": "FRAGRANCE",   "polygon": [[40,280],[460,280],[460,500],[40,500]]},
                  {"zone_id": "MAKEUP",      "polygon": [[480,280],[920,280],[920,500],[480,500]]},
              ]},
    "CAM_3": {"role": "FLOOR",   "frame_size": [_DEFAULT_W, _DEFAULT_H],
              "zones": [
                  {"zone_id": "HAIRCARE", "polygon": [[20,20],[940,20],[940,260],[20,260]]},
                  {"zone_id": "BODYCARE", "polygon": [[20,280],[940,280],[940,520],[20,520]]},
              ]},
    "CAM_4": {"role": "BILLING", "frame_size": [_DEFAULT_W, _DEFAULT_H],
              "zones": [{"zone_id": "BILLING", "polygon": [[80,40],[880,40],[880,500],[80,500]]}]},
    "CAM_5": {"role": "BILLING", "frame_size": [_DEFAULT_W, _DEFAULT_H],
              "zones": [{"zone_id": "BILLING", "polygon": [[80,40],[880,40],[880,500],[80,500]]}]},
}


def _camera_config(cam_id: str, store_id: Optional[str] = None) -> dict:
    sid = _resolve_store_id(store_id)
    layout = _load_layout(sid)
    layout_id = _short_to_layout(sid).get(cam_id, cam_id)
    if layout_id in layout:
        cfg = dict(layout[layout_id])
        cfg.setdefault("frame_size", [_DEFAULT_W, _DEFAULT_H])
        return cfg
    if cam_id in layout:
        cfg = dict(layout[cam_id])
        cfg.setdefault("frame_size", [_DEFAULT_W, _DEFAULT_H])
        return cfg
    if cam_id in _FALLBACK_CAMERAS:
        return dict(_FALLBACK_CAMERAS[cam_id])
    return dict(_FALLBACK_CAMERAS["CAM_2"])


# ---------------------------------------------------------------------
# Real-clip mode
# ---------------------------------------------------------------------
_CLIPS_ROOTS = ["/data/clips", "data/clips"]
_DETECTIONS_ROOTS = ["/data/detections", "data/detections"]


def _store_clip_dirs(store_id: str) -> List[str]:
    sub = _store_block(store_id).get("_clips_subdir")
    if sub:
        return [str(Path(r) / sub) for r in _CLIPS_ROOTS]
    return list(_CLIPS_ROOTS)


def _store_detection_dirs(store_id: str) -> List[str]:
    sub = _store_block(store_id).get("_detections_subdir")
    if sub:
        return [str(Path(r) / sub) for r in _DETECTIONS_ROOTS]
    return list(_DETECTIONS_ROOTS)


def _find_clip_for(cam_id: str, store_id: Optional[str] = None) -> Optional[Path]:
    sid = _resolve_store_id(store_id)
    layout_id = _short_to_layout(sid).get(cam_id, cam_id)
    name_to_camera = _store_block(sid).get("clip_to_camera") or {}

    for d in _store_clip_dirs(sid):
        if not os.path.isdir(d):
            continue
        # Match via clip_to_camera first (canonical).
        for clip_name, layout_cam in name_to_camera.items():
            if layout_cam == layout_id:
                f = Path(d) / clip_name
                if f.exists():
                    return f
        # Fallback by stem: "CAM 1.mp4" ↔ "CAM_1".
        for f in Path(d).glob("*.mp4"):
            if f.stem.replace(" ", "_").upper() == cam_id.upper():
                return f
    return None


def _find_detections_for(cam_id: str, store_id: Optional[str] = None) -> Optional[Path]:
    sid = _resolve_store_id(store_id)
    # Detection JSON files are named after the canonical layout id
    # (precompute_detections.py uses clip_to_camera → that id).
    layout_id = _short_to_layout(sid).get(cam_id, cam_id)
    candidates = [layout_id, cam_id]
    for d in _store_detection_dirs(sid):
        for c in candidates:
            f = Path(d) / f"{c}.json"
            if f.exists():
                return f
    return None


def _load_detections(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _index_detections(det: dict) -> Dict[int, list]:
    """frame index → list of [x1,y1,x2,y2,conf,track_id]"""
    return {int(fr["i"]): fr["boxes"] for fr in det.get("frames", [])}


# ---------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------
def _font():
    try:
        from PIL import ImageFont
        for path in [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]:
            if os.path.exists(path):
                return ImageFont.truetype(path, 16)
        return ImageFont.load_default()
    except Exception:
        return None


_FONT = _font()


# Distinct, accessible colours per track id.
_TRACK_COLOURS = [
    (52, 211, 153),   # mint
    (96, 165, 250),   # blue
    (251, 191, 36),   # amber
    (244, 114, 182),  # pink
    (167, 139, 250),  # violet
    (251, 113, 133),  # rose
    (74, 222, 128),   # emerald
    (251, 146, 60),   # orange
]


def _track_colour(tid: int) -> Tuple[int, int, int]:
    return _TRACK_COLOURS[tid % len(_TRACK_COLOURS)]


def _draw_overlays(draw, fw: int, fh: int, role: str, zones: List[dict]) -> None:
    """Zone polygons + entry line. Translucent, doesn't kill the underlying frame."""
    palette = [
        (52, 211, 153, 50),
        (96, 165, 250, 50),
        (251, 191, 36, 50),
        (244, 114, 182, 50),
        (167, 139, 250, 50),
    ]
    for i, z in enumerate(zones):
        poly = [(int(x), int(y)) for x, y in (z.get("polygon") or [])]
        if len(poly) >= 3:
            col = palette[i % len(palette)]
            draw.polygon(poly, fill=col, outline=(col[0], col[1], col[2], 230))
            draw.text((poly[0][0] + 6, poly[0][1] + 4),
                      z.get("zone_id", ""), fill=(245, 245, 245), font=_FONT)
    if role == "ENTRY":
        ly = int(fh / 2)
        draw.line([(0, ly), (fw, ly)], fill=(244, 63, 94, 230), width=2)
        draw.text((10, ly - 22), "ENTRY LINE", fill=(244, 63, 94), font=_FONT)


def _draw_hud(draw, fw: int, cam_id: str, role: str, frame_idx: int,
              fps: float, persons: int, mode: str) -> None:
    h = 30
    draw.rectangle([0, 0, fw, h], fill=(0, 0, 0, 200))
    badge_col = (52, 211, 153) if mode == "real" else (251, 191, 36)
    badge = "● LIVE (real)" if mode == "real" else "● LIVE (sim)"
    draw.text((10, 7), badge, fill=badge_col, font=_FONT)
    text = (f"{cam_id} | {role} | YOLOv8n | "
            f"persons={persons} | frame={frame_idx} | {fps:.1f} fps")
    draw.text((140, 7), text, fill=(230, 230, 230), font=_FONT)


def _draw_bbox(draw, x1, y1, x2, y2, conf: float, tid: int) -> None:
    col = _track_colour(tid)
    # Box.
    draw.rectangle([x1, y1, x2, y2], outline=col, width=3)
    # Label background + text.
    label = f"id:{tid}  {conf*100:.0f}%"
    tw = 10 * len(label)
    draw.rectangle([x1, max(0, y1 - 22), x1 + tw, y1],
                   fill=(0, 0, 0, 200))
    draw.text((x1 + 4, max(0, y1 - 20)), label, fill=col, font=_FONT)


# ---------------------------------------------------------------------
# Synthetic mode (fallback)
# ---------------------------------------------------------------------
@dataclass
class _Actor:
    actor_id: int
    cx: float; cy: float
    vx: float; vy: float
    w: int; h: int
    conf_base: float

    def step(self, dt: float, fw: int, fh: int) -> None:
        self.cx += self.vx * dt
        self.cy += self.vy * dt
        if self.cx < self.w / 2 or self.cx > fw - self.w / 2:
            self.vx *= -1
            self.cx = max(self.w / 2, min(fw - self.w / 2, self.cx))
        if self.cy < self.h / 2 or self.cy > fh - self.h / 2:
            self.vy *= -1
            self.cy = max(self.h / 2, min(fh - self.h / 2, self.cy))


def _seed_actors(cam_id: str, fw: int, fh: int, store_id: Optional[str] = None) -> List[_Actor]:
    role = (_camera_config(cam_id, store_id) or {}).get("role", "FLOOR")
    n = {"ENTRY": 2, "FLOOR": 4, "BILLING": 5}.get(role, 3)
    return [
        _Actor(
            actor_id=i + 1,
            cx=80 + (i * 180) % (fw - 120),
            cy=80 + (i * 130) % (fh - 120),
            vx=40 + (i * 17) % 60,
            vy=30 + (i * 23) % 50,
            w=70 + (i * 7) % 30,
            h=130 + (i * 11) % 40,
            conf_base=0.55 + (i * 0.07) % 0.40,
        )
        for i in range(n)
    ]


def _render_synth(cam_id: str, frame_idx: int, actors: List[_Actor],
                  fps: float, fw: int, fh: int, role: str,
                  zones: List[dict]) -> bytes:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (fw, fh), color=(15, 17, 23))
    draw = ImageDraw.Draw(img, "RGBA")
    for x in range(0, fw, 60):
        draw.line([(x, 0), (x, fh)], fill=(30, 35, 45), width=1)
    for y in range(0, fh, 60):
        draw.line([(0, y), (fw, y)], fill=(30, 35, 45), width=1)
    _draw_overlays(draw, fw, fh, role, zones)
    persons = 0
    for a in actors:
        x1 = int(a.cx - a.w / 2); y1 = int(a.cy - a.h / 2)
        x2 = int(a.cx + a.w / 2); y2 = int(a.cy + a.h / 2)
        conf = max(0.0, min(1.0,
                            a.conf_base + 0.05 * math.sin((frame_idx + a.actor_id) / 7.0)))
        _draw_bbox(draw, x1, y1, x2, y2, conf, a.actor_id)
        persons += 1
    _draw_hud(draw, fw, cam_id, role, frame_idx, fps, persons, "sim")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=72)
    return out.getvalue()


# ---------------------------------------------------------------------
# Real-frame renderer
# ---------------------------------------------------------------------
def _render_real(frame_bgr, frame_idx: int, boxes: list, fps: float,
                 cam_id: str, role: str, zones: List[dict]) -> bytes:
    from PIL import Image, ImageDraw
    import numpy as np  # type: ignore
    # cv2 returns BGR; convert to RGB for PIL.
    rgb = frame_bgr[..., ::-1].copy()
    img = Image.fromarray(rgb)
    fw, fh = img.size
    draw = ImageDraw.Draw(img, "RGBA")
    _draw_overlays(draw, fw, fh, role, zones)
    persons = 0
    for b in boxes or []:
        x1, y1, x2, y2, conf, tid = b
        _draw_bbox(draw, x1, y1, x2, y2, float(conf), int(tid))
        persons += 1
    _draw_hud(draw, fw, cam_id, role, frame_idx, fps, persons, "real")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=72)
    return out.getvalue()


# ---------------------------------------------------------------------
# Generators (MJPEG)
# ---------------------------------------------------------------------
_BOUNDARY = "frame"


def _wrap_jpeg(jpg: bytes) -> bytes:
    return (
        f"--{_BOUNDARY}\r\n".encode()
        + b"Content-Type: image/jpeg\r\n"
        + f"Content-Length: {len(jpg)}\r\n\r\n".encode()
        + jpg + b"\r\n"
    )


def _stream_real(clip_path: Path, det_path: Path, cam_id: str,
                 store_id: str,
                 target_fps: float, max_frames: Optional[int]) -> Iterable[bytes]:
    import cv2  # type: ignore
    det = _load_detections(det_path)
    by_frame = _index_detections(det)
    cfg = _camera_config(cam_id, store_id)
    role = cfg.get("role", "FLOOR")
    zones = cfg.get("zones") or []

    src_fps = float(det.get("fps") or 30.0)
    stride = int(det.get("stride") or 1)

    # Auto target fps: match the source unless the caller explicitly
    # asks for slower playback. This is what kills the "0.5x slow"
    # feel — at stride=5 we still play every frame at 30 fps; the
    # bbox positions interpolate linearly between detections so the
    # rectangle slides smoothly instead of jumping every 5 frames.
    play_fps = target_fps if target_fps > 0 else src_fps
    period = 1.0 / max(1.0, play_fps)

    # Build a sorted list of detection frame indices for fast lookup.
    det_indices = sorted(by_frame.keys())

    def _interp_boxes(idx: int) -> list:
        """Linear interpolation in image coordinates between adjacent
        detection samples. Tracks only match across the same track_id;
        a track that disappears in the next sample fades out by
        keeping its last position."""
        if not det_indices:
            return []
        # Find the bracket [lo, hi] of detection samples around idx.
        import bisect
        pos = bisect.bisect_right(det_indices, idx) - 1
        lo = det_indices[pos] if pos >= 0 else det_indices[0]
        hi = det_indices[pos + 1] if pos + 1 < len(det_indices) else lo
        boxes_lo = by_frame.get(lo, [])
        boxes_hi = by_frame.get(hi, [])
        if hi == lo or hi - lo <= 0:
            return boxes_lo
        if idx <= lo:
            return boxes_lo
        if idx >= hi:
            return boxes_hi
        t = (idx - lo) / (hi - lo)
        # Index hi-side boxes by track_id for matching.
        hi_by_id = {int(b[5]): b for b in boxes_hi}
        out = []
        for b in boxes_lo:
            tid = int(b[5])
            if tid in hi_by_id:
                bh = hi_by_id[tid]
                # Linearly blend bbox + confidence.
                ix1 = b[0] + (bh[0] - b[0]) * t
                iy1 = b[1] + (bh[1] - b[1]) * t
                ix2 = b[2] + (bh[2] - b[2]) * t
                iy2 = b[3] + (bh[3] - b[3]) * t
                ic  = b[4] + (bh[4] - b[4]) * t
                out.append([ix1, iy1, ix2, iy2, ic, tid])
            else:
                # Track dropped at the next sample — keep last but
                # fade confidence so the box visibly dims out.
                out.append([b[0], b[1], b[2], b[3], b[4] * (1 - t), tid])
        # Tracks that newly appeared in `hi`: fade them in.
        for tid, bh in hi_by_id.items():
            if not any(int(b[5]) == tid for b in boxes_lo):
                out.append([bh[0], bh[1], bh[2], bh[3], bh[4] * t, tid])
        return out

    cap = cv2.VideoCapture(str(clip_path))
    if not cap.isOpened():
        return

    emitted = 0
    frame_idx = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                # Loop the clip seamlessly.
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                frame_idx = 0
                continue

            boxes = _interp_boxes(frame_idx)
            jpg = _render_real(frame, frame_idx, boxes, play_fps,
                               cam_id, role, zones)
            yield _wrap_jpeg(jpg)
            emitted += 1
            if max_frames is not None and emitted >= max_frames:
                return
            frame_idx += 1
            time.sleep(period)
    except (BrokenPipeError, ConnectionResetError, GeneratorExit):
        return
    finally:
        try:
            cap.release()
        except Exception:
            pass


def _stream_synth(cam_id: str, store_id: str, target_fps: float,
                  max_frames: Optional[int]) -> Iterable[bytes]:
    cfg = _camera_config(cam_id, store_id)
    fw, fh = cfg.get("frame_size") or [_DEFAULT_W, _DEFAULT_H]
    role = cfg.get("role", "FLOOR")
    zones = cfg.get("zones") or []
    actors = _seed_actors(cam_id, fw, fh, store_id)

    period = 1.0 / max(1.0, target_fps)
    frame_idx = 0
    last = time.time()
    emitted = 0
    try:
        while True:
            now = time.time()
            dt = now - last
            last = now
            for a in actors:
                a.step(dt, fw, fh)
            jpg = _render_synth(cam_id, frame_idx, actors, target_fps, fw, fh, role, zones)
            yield _wrap_jpeg(jpg)
            emitted += 1
            frame_idx += 1
            if max_frames is not None and emitted >= max_frames:
                return
            time.sleep(period)
    except (BrokenPipeError, ConnectionResetError, GeneratorExit):
        return


# ---------------------------------------------------------------------
# Public mode helpers (also used by /cameras to advertise capabilities)
# ---------------------------------------------------------------------
def camera_mode(cam_id: str, store_id: Optional[str] = None) -> dict:
    sid = _resolve_store_id(store_id)
    clip = _find_clip_for(cam_id, sid)
    det = _find_detections_for(cam_id, sid)
    if clip and det:
        try:
            d = _load_detections(det)
            n_pop = len(d.get("frames", []))
        except Exception:
            n_pop = 0
            d = {}
        return {
            "mode": "real",
            "clip": clip.name,
            "fps": d.get("fps"),
            "n_detected_frames": n_pop,
        }
    return {"mode": "sim"}


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@router.get("/cameras")
def list_cameras(store_id: Optional[str] = None) -> dict:
    """
    Cameras advertised by the API.

    Without `?store_id=` returns the cameras for the first store in the
    layout (the legacy STORE_BLR_002, so existing clients keep working).
    With `?store_id=...` returns the cameras for that store; unknown ids
    fall back to the default store rather than 404 — keeps the dashboard
    responsive while a freshly-added store is still being detected on.
    """
    sid = _resolve_store_id(store_id)
    cams = _public_camera_ids(sid)
    if not cams:
        # Layout is empty / unreadable — fall back to synthetic ids.
        cams = list(_FALLBACK_CAMERAS.keys())
    return {
        "store_id": sid,
        "stores": _list_store_ids(),
        "cameras": cams,
        "modes": {c: camera_mode(c, sid) for c in cams},
    }


@router.get("/cameras/stream/{cam_id}")
def camera_stream(cam_id: str, request: Request, fps: float = 0.0,
                  max_frames: Optional[int] = None,
                  mode: Optional[str] = None,
                  store_id: Optional[str] = None) -> StreamingResponse:
    """
    MJPEG stream.
      mode=real   → require clip + detections, else 503
      mode=sim    → force the synthetic renderer
      omitted     → auto: real if both files exist, else sim

    fps=0 (default) plays at the clip's native fps. fps>0 caps it
    (use a smaller number for slow-motion analysis on a weak machine).

    `store_id` selects which store's clip/detection set to draw from.
    Defaults to the first store declared in the layout.
    """
    sid = _resolve_store_id(store_id)
    clip = _find_clip_for(cam_id, sid)
    det = _find_detections_for(cam_id, sid)
    use_real = (mode == "real") or (mode is None and clip and det)

    if use_real and clip and det:
        gen = _stream_real(clip, det, cam_id, sid, target_fps=fps,
                           max_frames=max_frames)
    elif mode == "real":
        from fastapi import HTTPException
        raise HTTPException(
            status_code=503,
            detail=f"real mode unavailable for {cam_id} in {sid}: "
                   f"clip={bool(clip)}, detections={bool(det)}",
        )
    else:
        # Synthetic mode keeps a sensible default if the caller did
        # not specify a target rate.
        gen = _stream_synth(cam_id, sid, target_fps=(fps if fps > 0 else 12.0),
                            max_frames=max_frames)

    return StreamingResponse(
        gen,
        media_type=f"multipart/x-mixed-replace; boundary={_BOUNDARY}",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
