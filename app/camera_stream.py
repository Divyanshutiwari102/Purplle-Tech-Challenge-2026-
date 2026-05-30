"""
Simulated YOLO camera stream as MJPEG.

This is a *demo* stream — not real video. It draws synthetic bounding
boxes that move across a fake store frame so reviewers can see what the
detection layer would render in production. Zone polygons come from
`data/layout/store_layout.json` if present, otherwise sensible defaults.

Why MJPEG?
  - It is just multipart JPEG over plain HTTP — works in any <img>.
  - No transcoding, no ffmpeg, no GPU.
  - Browser handles the framing; we only need to emit jpeg blobs.

Why we draw it ourselves instead of replaying mp4?
  - The CCTV mp4s are challenge-licensed and never leave the local box.
  - The synthetic frame demonstrates the *system* (bboxes, zones, HUD,
    YOLO label) without exposing the source footage.
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
# Layout: zone polygons per camera
# ---------------------------------------------------------------------
_LAYOUT_PATH_CANDIDATES = [
    "/data/store_layout.json",
    "data/layout/store_layout.json",
    str(Path(__file__).resolve().parent.parent / "data" / "layout" / "store_layout.json"),
]


def _load_layout() -> Dict[str, dict]:
    for p in _LAYOUT_PATH_CANDIDATES:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    layout = json.load(f)
                # Take the first store; that's enough for the demo.
                stores = layout.get("stores") or {}
                if stores:
                    first = next(iter(stores.values()))
                    return first.get("cameras") or {}
            except Exception:
                pass
    return {}


_DEFAULT_FRAME_W = 960
_DEFAULT_FRAME_H = 540


# Built-in fallback so /cameras/stream/CAM_X always works.
_FALLBACK_CAMERAS: Dict[str, dict] = {
    "CAM_1": {"role": "ENTRY",   "frame_size": [_DEFAULT_FRAME_W, _DEFAULT_FRAME_H], "entry_line_y": 270, "zones": []},
    "CAM_2": {"role": "FLOOR",   "frame_size": [_DEFAULT_FRAME_W, _DEFAULT_FRAME_H],
              "zones": [
                  {"zone_id": "SKINCARE",    "polygon": [[40,40],[460,40],[460,260],[40,260]]},
                  {"zone_id": "MOISTURISER", "polygon": [[480,40],[920,40],[920,260],[480,260]]},
                  {"zone_id": "FRAGRANCE",   "polygon": [[40,280],[460,280],[460,500],[40,500]]},
                  {"zone_id": "MAKEUP",      "polygon": [[480,280],[920,280],[920,500],[480,500]]},
              ]},
    "CAM_3": {"role": "FLOOR",   "frame_size": [_DEFAULT_FRAME_W, _DEFAULT_FRAME_H],
              "zones": [
                  {"zone_id": "HAIRCARE", "polygon": [[20,20],[940,20],[940,260],[20,260]]},
                  {"zone_id": "BODYCARE", "polygon": [[20,280],[940,280],[940,520],[20,520]]},
              ]},
    "CAM_4": {"role": "BILLING", "frame_size": [_DEFAULT_FRAME_W, _DEFAULT_FRAME_H],
              "zones": [{"zone_id": "BILLING", "polygon": [[80,40],[880,40],[880,500],[80,500]]}]},
    "CAM_5": {"role": "BILLING", "frame_size": [_DEFAULT_FRAME_W, _DEFAULT_FRAME_H],
              "zones": [{"zone_id": "BILLING", "polygon": [[80,40],[880,40],[880,500],[80,500]]}]},
}


def _camera_config(cam_id: str) -> dict:
    layout = _load_layout()
    # Map "CAM_1" → "CAM_ENTRY_01" if such a layout exists; else use as-is.
    if cam_id in layout:
        cam = dict(layout[cam_id])
    elif cam_id in _FALLBACK_CAMERAS:
        cam = dict(_FALLBACK_CAMERAS[cam_id])
    else:
        cam = dict(_FALLBACK_CAMERAS["CAM_2"])  # generic fallback
        cam["_resolved_from"] = "default"
    cam.setdefault("frame_size", [_DEFAULT_FRAME_W, _DEFAULT_FRAME_H])
    return cam


# ---------------------------------------------------------------------
# Synthetic actors
# ---------------------------------------------------------------------
@dataclass
class Actor:
    actor_id: int
    cx: float
    cy: float
    vx: float
    vy: float
    w: int
    h: int
    conf_base: float

    def step(self, dt: float, fw: int, fh: int) -> None:
        self.cx += self.vx * dt
        self.cy += self.vy * dt
        # Bounce off frame edges.
        if self.cx < self.w / 2 or self.cx > fw - self.w / 2:
            self.vx *= -1
            self.cx = max(self.w / 2, min(fw - self.w / 2, self.cx))
        if self.cy < self.h / 2 or self.cy > fh - self.h / 2:
            self.vy *= -1
            self.cy = max(self.h / 2, min(fh - self.h / 2, self.cy))

    def bbox(self) -> Tuple[int, int, int, int]:
        return (
            int(self.cx - self.w / 2),
            int(self.cy - self.h / 2),
            int(self.cx + self.w / 2),
            int(self.cy + self.h / 2),
        )


def _seed_actors(cam_id: str, fw: int, fh: int) -> List[Actor]:
    # Different camera roles get different actor counts.
    role = (_camera_config(cam_id) or {}).get("role", "FLOOR")
    n = {"ENTRY": 2, "FLOOR": 4, "BILLING": 5}.get(role, 3)
    actors: List[Actor] = []
    for i in range(n):
        actors.append(Actor(
            actor_id=i + 1,
            cx=80 + (i * 180) % (fw - 120),
            cy=80 + (i * 130) % (fh - 120),
            vx=40 + (i * 17) % 60,
            vy=30 + (i * 23) % 50,
            w=70 + (i * 7) % 30,
            h=130 + (i * 11) % 40,
            conf_base=0.55 + (i * 0.07) % 0.40,
        ))
    return actors


# ---------------------------------------------------------------------
# Frame rendering (PIL)
# ---------------------------------------------------------------------
def _font():
    try:
        from PIL import ImageFont  # type: ignore
        # The Linux container often has DejaVuSans somewhere; if not,
        # ImageFont.load_default() always works.
        for path in [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]:
            if os.path.exists(path):
                return ImageFont.truetype(path, 14)
        return ImageFont.load_default()
    except Exception:
        return None


_FONT = _font()


def _render_frame(cam_id: str, frame_idx: int, actors: List[Actor],
                  fps: float, fw: int, fh: int, role: str,
                  zones: List[dict]) -> bytes:
    from PIL import Image, ImageDraw  # type: ignore

    img = Image.new("RGB", (fw, fh), color=(15, 17, 23))
    draw = ImageDraw.Draw(img, "RGBA")

    # Floor grid (visual texture so the frame doesn't look empty).
    for x in range(0, fw, 60):
        draw.line([(x, 0), (x, fh)], fill=(30, 35, 45), width=1)
    for y in range(0, fh, 60):
        draw.line([(0, y), (fw, y)], fill=(30, 35, 45), width=1)

    # Zone polygons (translucent).
    palette = [
        (52, 211, 153, 60),
        (96, 165, 250, 60),
        (251, 191, 36, 60),
        (244, 114, 182, 60),
        (167, 139, 250, 60),
    ]
    for i, z in enumerate(zones):
        poly = [(int(x), int(y)) for x, y in z.get("polygon") or []]
        if len(poly) >= 3:
            draw.polygon(poly, fill=palette[i % len(palette)],
                         outline=(palette[i % len(palette)][0],
                                  palette[i % len(palette)][1],
                                  palette[i % len(palette)][2], 255))
            label_pos = poly[0]
            draw.text((label_pos[0] + 6, label_pos[1] + 4),
                      z.get("zone_id", ""), fill=(230, 230, 230), font=_FONT)

    # Entry virtual line (only on ENTRY cameras).
    if role == "ENTRY":
        ly = int(fh / 2)
        draw.line([(0, ly), (fw, ly)], fill=(244, 63, 94, 255), width=2)
        draw.text((10, ly - 18), "ENTRY LINE", fill=(244, 63, 94), font=_FONT)

    # Bounding boxes for synthetic "people".
    for a in actors:
        x1, y1, x2, y2 = a.bbox()
        # Confidence wobbles slightly per frame for realism.
        conf = max(0.0, min(1.0,
                            a.conf_base + 0.05 * math.sin((frame_idx + a.actor_id) / 7.0)))
        col = (52, 211, 153) if conf >= 0.6 else (251, 191, 36)
        draw.rectangle([x1, y1, x2, y2], outline=col, width=2)
        label = f"person #{a.actor_id}  {conf:.2f}"
        draw.rectangle([x1, max(0, y1 - 18), x1 + 8 * len(label), y1],
                       fill=(0, 0, 0, 180))
        draw.text((x1 + 2, max(0, y1 - 16)), label, fill=col, font=_FONT)

    # HUD (top strip).
    hud_h = 26
    draw.rectangle([0, 0, fw, hud_h], fill=(0, 0, 0, 200))
    hud = (f"{cam_id} | {role} | YOLOv8n (sim) | "
           f"persons={len(actors)} | frame={frame_idx} | {fps:.1f} fps")
    draw.text((8, 6), hud, fill=(230, 230, 230), font=_FONT)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=70)
    return out.getvalue()


# ---------------------------------------------------------------------
# MJPEG generator
# ---------------------------------------------------------------------
_BOUNDARY = "frame"


def _mjpeg(cam_id: str, request: Request, target_fps: float = 8.0,
           max_frames: Optional[int] = None) -> Iterable[bytes]:
    cfg = _camera_config(cam_id)
    fw, fh = cfg.get("frame_size") or [_DEFAULT_FRAME_W, _DEFAULT_FRAME_H]
    role = cfg.get("role", "FLOOR")
    zones = cfg.get("zones") or []
    actors = _seed_actors(cam_id, fw, fh)

    period = 1.0 / max(1.0, target_fps)
    frame_idx = 0
    last = time.time()
    try:
        while True:
            now = time.time()
            dt = now - last
            last = now
            for a in actors:
                a.step(dt, fw, fh)
            jpg = _render_frame(cam_id, frame_idx, actors, target_fps, fw, fh, role, zones)
            yield (
                f"--{_BOUNDARY}\r\n".encode()
                + b"Content-Type: image/jpeg\r\n"
                + f"Content-Length: {len(jpg)}\r\n\r\n".encode()
                + jpg
                + b"\r\n"
            )
            frame_idx += 1
            if max_frames is not None and frame_idx >= max_frames:
                return
            time.sleep(period)
    except (BrokenPipeError, ConnectionResetError, GeneratorExit):
        return


@router.get("/cameras/stream/{cam_id}")
def camera_stream(cam_id: str, request: Request, fps: float = 8.0,
                  max_frames: Optional[int] = None) -> StreamingResponse:
    """
    MJPEG stream for the simulated camera <cam_id>.

    Drop a `<img src=".../cameras/stream/CAM_1" />` in any HTML and you
    will see synthetic bounding boxes moving over zone overlays with
    a YOLOv8n-style HUD.

    `max_frames` is for tests: bounds the generator so the suite does
    not hang on the infinite stream.
    """
    return StreamingResponse(
        _mjpeg(cam_id, request, target_fps=fps, max_frames=max_frames),
        media_type=f"multipart/x-mixed-replace; boundary={_BOUNDARY}",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/cameras")
def list_cameras() -> dict:
    """List the cameras available to stream."""
    layout = _load_layout()
    cams = list(layout.keys()) or list(_FALLBACK_CAMERAS.keys())
    return {"cameras": cams}
