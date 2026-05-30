"""
Detection pipeline.

Reads CCTV clips, runs YOLOv8 person detection, tracks bounding boxes
with ByteTrack-style ID assignment, and emits structured events.

Design notes:
  • YOLOv8 + ByteTrack are imported lazily so that unit tests (which
    monkey-patch detection) and machines without GPUs can still import
    this module.
  • Frame-level decisions live in this file; cross-frame state (Re-ID,
    staff heuristic) lives in tracker.py.
  • Entry/exit detection uses a virtual line from store_layout.json
    and requires the centroid to cross it for 3 consecutive frames to
    confirm direction. This kills boundary jitter.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import defaultdict, deque  # noqa: F401
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from .emit import JSONLEmitter, make_event
from .tracker import Tracker

log = logging.getLogger("detect")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Confidence below this is still emitted (per spec) but flagged downstream.
LOW_CONF_FLAG = 0.6
# How many consecutive frames a centroid must remain on the post-line side.
LINE_CROSS_CONFIRM = 3
# Queue thresholds.
QUEUE_MIN_DEPTH = 3
ABANDON_WINDOW = timedelta(minutes=5)
DWELL_EMIT_INTERVAL = timedelta(seconds=30)
EMPTY_LOG_INTERVAL = timedelta(minutes=10)
ZONE_DEDUP_WINDOW = timedelta(seconds=2)


# ---------------------------------------------------------------------
@dataclass
class CameraConfig:
    camera_id: str
    role: str                        # 'ENTRY' | 'FLOOR' | 'BILLING'
    entry_line_y: Optional[int] = None  # only for ENTRY cameras
    zones: List[Dict[str, Any]] = None  # list of {zone_id, polygon}

    @property
    def is_entry(self) -> bool:
        return self.role.upper() == "ENTRY"

    @property
    def is_billing(self) -> bool:
        return self.role.upper() == "BILLING"


def load_layout(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------
# Per-track per-camera state for line crossing
# ---------------------------------------------------------------------
@dataclass
class _LineState:
    side_history: Deque[str] = None  # 'above' / 'below'
    confirmed_direction: Optional[str] = None  # 'ENTRY' | 'EXIT'

    def __post_init__(self):
        if self.side_history is None:
            self.side_history = deque(maxlen=LINE_CROSS_CONFIRM)


def _classify_side(y: float, line_y: float) -> str:
    return "above" if y < line_y else "below"


# ---------------------------------------------------------------------
# Geometry: point-in-polygon (ray casting). No external deps.
# ---------------------------------------------------------------------
def point_in_polygon(x: float, y: float, polygon: List[List[float]]) -> bool:
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi):
            inside = not inside
        j = i
    return inside


# ---------------------------------------------------------------------
# Detection backends
# ---------------------------------------------------------------------
class _NullDetector:
    """Returned when YOLO isn't available. Lets the rest of the pipeline boot."""

    def detect(self, frame, conf: float = 0.25):
        return []


def _load_yolo(weights: str = "yolov8n.pt"):
    try:
        from ultralytics import YOLO  # type: ignore
    except ImportError:
        log.warning("ultralytics not installed — running with null detector")
        return _NullDetector()

    model = YOLO(weights)

    class _YoloDetector:
        def detect(self, frame, conf: float = 0.25):
            results = model(frame, classes=[0], conf=conf, verbose=False)
            out = []
            for r in results:
                for box in r.boxes:
                    x1, y1, x2, y2 = [float(v) for v in box.xyxy[0].tolist()]
                    c = float(box.conf[0])
                    out.append((x1, y1, x2, y2, c))
            return out

    return _YoloDetector()


# ---------------------------------------------------------------------
# A simple IoU tracker for offline use when ByteTrack isn't installed.
# ---------------------------------------------------------------------
def _iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1 = max(ax1, bx1); iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2); iy2 = min(ay2, by2)
    iw = max(0.0, ix2 - ix1); ih = max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    aa = (ax2 - ax1) * (ay2 - ay1)
    bb = (bx2 - bx1) * (by2 - by1)
    return inter / (aa + bb - inter)


class IoUTracker:
    """A pragmatic stand-in for ByteTrack: per-frame greedy IoU matching."""

    def __init__(self, iou_threshold: float = 0.3, max_missed: int = 30):
        self.iou_threshold = iou_threshold
        self.max_missed = max_missed
        self._tracks: Dict[int, Dict[str, Any]] = {}
        self._next_id = 1

    def update(self, detections: List[Tuple[float, float, float, float, float]]) -> List[Dict[str, Any]]:
        live = list(self._tracks.items())
        unmatched_dets = list(range(len(detections)))
        matches: List[Tuple[int, int]] = []

        for tid, t in live:
            best_iou, best_j = 0.0, -1
            for j in unmatched_dets:
                d = detections[j][:4]
                iou = _iou(t["bbox"], d)
                if iou > best_iou:
                    best_iou, best_j = iou, j
            if best_iou >= self.iou_threshold and best_j >= 0:
                matches.append((tid, best_j))
                unmatched_dets.remove(best_j)

        # Update matched
        for tid, j in matches:
            x1, y1, x2, y2, c = detections[j]
            self._tracks[tid].update(bbox=(x1, y1, x2, y2), conf=c, missed=0)

        # New tracks for unmatched detections
        for j in unmatched_dets:
            x1, y1, x2, y2, c = detections[j]
            self._tracks[self._next_id] = {"bbox": (x1, y1, x2, y2), "conf": c, "missed": 0}
            self._next_id += 1

        # Increment missed; drop stale
        matched_ids = {tid for tid, _ in matches} | {tid for tid in self._tracks if self._tracks[tid].get("missed", 0) == 0}
        out = []
        for tid in list(self._tracks.keys()):
            if tid in matched_ids:
                t = self._tracks[tid]
                out.append({"track_id": tid, "bbox": t["bbox"], "conf": t["conf"]})
            else:
                self._tracks[tid]["missed"] = self._tracks[tid].get("missed", 0) + 1
                if self._tracks[tid]["missed"] > self.max_missed:
                    del self._tracks[tid]
        return out


# ---------------------------------------------------------------------
# Cheap appearance embedding: colour histogram of bbox crop.
# ---------------------------------------------------------------------
def _embedding(frame, bbox) -> Optional["any"]:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        return None
    x1, y1, x2, y2 = [int(max(0, v)) for v in bbox]
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    return hist.flatten().astype("float32")


# ---------------------------------------------------------------------
# The main pipeline
# ---------------------------------------------------------------------
class DetectionPipeline:
    def __init__(self, store_id: str, layout: Dict[str, Any], emitter: JSONLEmitter,
                 weights: str = "yolov8n.pt", conf_threshold: float = 0.25):
        self.store_id = store_id
        self.layout = layout
        self.emitter = emitter
        self.detector = _load_yolo(weights)
        self.conf_threshold = conf_threshold

        self.tracker = Tracker(store_id)
        self._iou_trackers: Dict[str, IoUTracker] = defaultdict(IoUTracker)

        # Per (camera, track_id) → visitor_id
        self._track_to_visitor: Dict[Tuple[str, int], str] = {}

        # Line-crossing state per (camera, track_id)
        self._line_state: Dict[Tuple[str, int], _LineState] = {}

        # Zone state per visitor: zone_id -> entered_at
        self._zone_open: Dict[str, Dict[str, datetime]] = defaultdict(dict)
        self._zone_last_dwell: Dict[Tuple[str, str], datetime] = {}

        # Cross-camera dedup for ZONE_ENTER
        self._recent_zone_enter: Dict[Tuple[str, str], datetime] = {}

        # Billing queue state
        self._billing_present: Dict[str, datetime] = {}     # visitor_id -> joined_at
        self._billing_pending_abandon: Dict[str, datetime] = {}
        self._last_queue_depth: int = 0

        # Bookkeeping
        self._last_detection_at: Optional[datetime] = None
        self._last_empty_log: Optional[datetime] = None
        self._session_seq: Dict[str, int] = defaultdict(int)

    # -----------------------------------------------------------------
    def _seq(self, visitor_id: str) -> int:
        self._session_seq[visitor_id] += 1
        return self._session_seq[visitor_id]

    # -----------------------------------------------------------------
    def _camera_config(self, camera_id: str) -> CameraConfig:
        cam = self.layout["cameras"][camera_id]
        return CameraConfig(
            camera_id=camera_id,
            role=cam.get("role", "FLOOR"),
            entry_line_y=cam.get("entry_line_y"),
            zones=cam.get("zones") or [],
        )

    # -----------------------------------------------------------------
    def _zone_for_point(self, cam: CameraConfig, x: float, y: float) -> Optional[str]:
        for z in cam.zones or []:
            if point_in_polygon(x, y, z["polygon"]):
                return z["zone_id"]
        return None

    # -----------------------------------------------------------------
    def _maybe_emit(self, **kw) -> None:
        ev = make_event(store_id=self.store_id, **kw)
        # Stamp is_staff from the tracker so events for a visitor flagged
        # later still get marked correctly.
        ev["is_staff"] = self.tracker.is_staff(kw["visitor_id"])
        self.emitter.emit(ev)

    # -----------------------------------------------------------------
    def process_frame(self, frame, camera_id: str, ts: datetime) -> None:
        cam = self._camera_config(camera_id)
        detections = self.detector.detect(frame, conf=self.conf_threshold)

        if not detections:
            self._maybe_log_empty(ts)
        else:
            self._last_detection_at = ts

        tracked = self._iou_trackers[camera_id].update(detections)

        for t in tracked:
            track_id = t["track_id"]
            x1, y1, x2, y2 = t["bbox"]
            cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            conf = t["conf"]

            key = (camera_id, track_id)
            visitor_id = self._track_to_visitor.get(key)

            # ENTRY camera: line-crossing logic
            if cam.is_entry and cam.entry_line_y is not None:
                ls = self._line_state.setdefault(key, _LineState())
                ls.side_history.append(_classify_side(cy, cam.entry_line_y))
                if len(ls.side_history) == LINE_CROSS_CONFIRM:
                    if all(s == "below" for s in ls.side_history) and ls.confirmed_direction != "ENTRY":
                        # Crossed inward.
                        if visitor_id is None:
                            emb = _embedding(frame, (x1, y1, x2, y2))
                            visitor_id, is_reentry = self.tracker.assign(
                                embedding=emb,
                                entry_region=camera_id,
                                ts=ts,
                                camera_id=camera_id,
                            )
                            self._track_to_visitor[key] = visitor_id
                            self._maybe_emit(
                                camera_id=camera_id, visitor_id=visitor_id,
                                event_type="REENTRY" if is_reentry else "ENTRY",
                                timestamp=ts, confidence=conf,
                                session_seq=self._seq(visitor_id),
                            )
                        ls.confirmed_direction = "ENTRY"
                    elif all(s == "above" for s in ls.side_history) and ls.confirmed_direction != "EXIT":
                        if visitor_id is not None:
                            self._maybe_emit(
                                camera_id=camera_id, visitor_id=visitor_id,
                                event_type="EXIT", timestamp=ts, confidence=conf,
                                session_seq=self._seq(visitor_id),
                            )
                            self.tracker.close(visitor_id, ts)
                        ls.confirmed_direction = "EXIT"

            # FLOOR/BILLING cameras: zone enter/exit/dwell
            if not cam.is_entry:
                # In this dataset each clip is independent: ENTRY camera
                # sees the door, FLOOR/BILLING see the same store from
                # different angles but we have no global re-id across them.
                # We assign a per-camera local visitor_id so we can still
                # emit zone events. Cross-camera dedup happens on the API
                # side when /funnel collapses by visitor_id.
                if visitor_id is None:
                    emb = _embedding(frame, (x1, y1, x2, y2))
                    visitor_id, _ = self.tracker.assign(
                        embedding=emb,
                        entry_region=camera_id,
                        ts=ts,
                        camera_id=camera_id,
                    )
                    self._track_to_visitor[key] = visitor_id

            if visitor_id is not None:
                self.tracker.touch(visitor_id, ts, camera_id)
                zone_id = self._zone_for_point(cam, cx, cy)
                self._update_zone_state(visitor_id, zone_id, ts, conf, camera_id)
                if cam.is_billing and zone_id and zone_id.startswith("BILLING"):
                    self._update_billing_state(visitor_id, ts, conf, camera_id)

    # -----------------------------------------------------------------
    def _update_zone_state(self, visitor_id: str, zone_id: Optional[str],
                           ts: datetime, conf: float,
                           camera_id: str = "FLOOR") -> None:
        open_zones = self._zone_open[visitor_id]

        # Exit zones we left.
        for z, entered_at in list(open_zones.items()):
            if z != zone_id:
                self._maybe_emit(
                    camera_id=camera_id, visitor_id=visitor_id,
                    event_type="ZONE_EXIT", timestamp=ts, zone_id=z,
                    dwell_ms=int((ts - entered_at).total_seconds() * 1000),
                    confidence=conf, sku_zone=z,
                    session_seq=self._seq(visitor_id),
                )
                del open_zones[z]

        if zone_id is None:
            return

        if zone_id not in open_zones:
            # Cross-camera dedup: if we just emitted ZONE_ENTER for this
            # (visitor, zone) from another camera, suppress.
            ck = (visitor_id, zone_id)
            recent = self._recent_zone_enter.get(ck)
            if not recent or (ts - recent) > ZONE_DEDUP_WINDOW:
                self._maybe_emit(
                    camera_id=camera_id, visitor_id=visitor_id,
                    event_type="ZONE_ENTER", timestamp=ts, zone_id=zone_id,
                    confidence=conf, sku_zone=zone_id,
                    session_seq=self._seq(visitor_id),
                )
                self._recent_zone_enter[ck] = ts
            open_zones[zone_id] = ts

        # ZONE_DWELL: emit every 30s of continued presence.
        last = self._zone_last_dwell.get((visitor_id, zone_id))
        if (ts - open_zones[zone_id]) >= DWELL_EMIT_INTERVAL and (
            last is None or (ts - last) >= DWELL_EMIT_INTERVAL
        ):
            self._maybe_emit(
                camera_id=camera_id, visitor_id=visitor_id,
                event_type="ZONE_DWELL", timestamp=ts, zone_id=zone_id,
                dwell_ms=int((ts - open_zones[zone_id]).total_seconds() * 1000),
                confidence=conf, sku_zone=zone_id,
                session_seq=self._seq(visitor_id),
            )
            self._zone_last_dwell[(visitor_id, zone_id)] = ts

    # -----------------------------------------------------------------
    def _update_billing_state(self, visitor_id: str, ts: datetime, conf: float,
                              camera_id: str = "BILLING") -> None:
        # Emit BILLING_QUEUE_JOIN only when this visitor first enters the
        # billing zone in the current visit (not on every frame).
        already_in_queue = visitor_id in self._billing_present
        self._billing_present[visitor_id] = ts
        self.tracker.record_billing_crossing(visitor_id)
        depth = len(self._billing_present)
        if not already_in_queue and depth >= QUEUE_MIN_DEPTH:
            self._maybe_emit(
                camera_id=camera_id, visitor_id=visitor_id,
                event_type="BILLING_QUEUE_JOIN", timestamp=ts,
                zone_id="BILLING", confidence=conf,
                queue_depth=depth, sku_zone="BILLING",
                session_seq=self._seq(visitor_id),
            )
        self._last_queue_depth = depth

    def check_abandonments(self, ts: datetime, transactions: List[datetime] = None) -> None:
        """Call periodically with the latest transactions for the store."""
        transactions = transactions or []
        for vid, joined_at in list(self._billing_present.items()):
            # Person has left the billing zone in the data: handled when they
            # appear in another zone (zone_state). Here we look for stale entries.
            if (ts - joined_at) > ABANDON_WINDOW:
                txn_after = any(joined_at <= t <= ts for t in transactions)
                if not txn_after:
                    self._maybe_emit(
                        camera_id="BILLING", visitor_id=vid,
                        event_type="BILLING_QUEUE_ABANDON", timestamp=ts,
                        zone_id="BILLING", confidence=0.9,
                        sku_zone="BILLING", session_seq=self._seq(vid),
                    )
                self._billing_present.pop(vid, None)

    # -----------------------------------------------------------------
    def _maybe_log_empty(self, ts: datetime) -> None:
        if self._last_detection_at is None:
            return
        gap = ts - self._last_detection_at
        if gap >= EMPTY_LOG_INTERVAL and (
            self._last_empty_log is None or (ts - self._last_empty_log) >= EMPTY_LOG_INTERVAL
        ):
            log.info("empty_store store=%s gap_minutes=%.1f", self.store_id, gap.total_seconds() / 60)
            self._last_empty_log = ts


# ---------------------------------------------------------------------
# CLI: process clips for a store
# ---------------------------------------------------------------------
def _camera_id_from_filename(name: str, store_layout: Dict[str, Any]) -> str:
    """
    Resolve a clip filename to a camera_id.

    Order of resolution:
      1. Exact match in store_layout['clip_to_camera'] (preferred).
      2. Substring match: any cameras key that appears in the filename.
      3. Substring match against the underscore-normalised filename.
      4. Fall back to first declared camera (logged as warning).
    """
    mapping = store_layout.get("clip_to_camera", {}) or {}
    if name in mapping:
        return mapping[name]

    cameras = store_layout.get("cameras", {})
    for cid in cameras:
        if cid in name:
            return cid

    norm = name.replace(" ", "_").upper()
    for cid in cameras:
        if cid.upper() in norm:
            return cid

    fallback = next(iter(cameras))
    log.warning("could not resolve camera for clip=%s; using fallback=%s", name, fallback)
    return fallback


def _process_clip(pipeline: "DetectionPipeline", clip_path: Path,
                  camera_id: str, frame_stride: int = 1) -> int:
    """Process a single clip end-to-end. Returns frame count consumed."""
    import cv2  # type: ignore
    cap = cv2.VideoCapture(str(clip_path))
    if not cap.isOpened():
        log.error("cannot open clip=%s", clip_path)
        return 0

    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    log.info("processing clip=%s camera=%s fps=%.1f frames=%d stride=%d",
             clip_path.name, camera_id, fps, n_frames, frame_stride)

    # Anchor synthetic timestamps in the recent past so the API "today"
    # window covers them. Estimated clip duration drives the offset.
    clip_seconds = (n_frames / fps) if fps else 0
    clip_start = datetime.now(timezone.utc) - timedelta(
        seconds=max(60, clip_seconds + 30)
    )

    frame_idx = 0
    processed = 0
    last_log = time.time()
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_stride > 1 and frame_idx % frame_stride != 0:
            frame_idx += 1
            continue
        ts = clip_start + timedelta(seconds=frame_idx / fps)
        pipeline.process_frame(frame, camera_id, ts)
        frame_idx += 1
        processed += 1
        if time.time() - last_log > 10:
            log.info("  ... %s frame %d/%d (%.0f%%)",
                     clip_path.name, frame_idx, n_frames,
                     (100.0 * frame_idx / n_frames) if n_frames else 0)
            last_log = time.time()

    cap.release()
    pipeline.check_abandonments(clip_start + timedelta(seconds=frame_idx / fps))
    log.info("done clip=%s processed_frames=%d", clip_path.name, processed)
    return processed


def run(store_id: str, layout_path: str, out_dir: str,
        clips_dir: str | None = None, video: str | None = None,
        weights: str = "yolov8n.pt", frame_stride: int = 1) -> None:
    try:
        import cv2  # type: ignore  # noqa: F401
    except ImportError:
        log.error("opencv-python not installed; cannot read video. pip install opencv-python")
        sys.exit(2)

    layout = load_layout(layout_path)
    store_layout = layout["stores"][store_id]
    emitter = JSONLEmitter(out_dir, store_id)
    pipeline = DetectionPipeline(store_id, store_layout, emitter, weights=weights)

    try:
        if video:
            clip_path = Path(video)
            if not clip_path.exists():
                log.error("video not found: %s", clip_path)
                sys.exit(2)
            camera_id = _camera_id_from_filename(clip_path.name, store_layout)
            _process_clip(pipeline, clip_path, camera_id, frame_stride)
        else:
            cdir = Path(clips_dir or "data/clips")
            clips = sorted(cdir.glob("*.mp4"))
            if not clips:
                log.error("no .mp4 clips found in %s", cdir)
                sys.exit(2)
            for clip in clips:
                camera_id = _camera_id_from_filename(clip.name, store_layout)
                _process_clip(pipeline, clip, camera_id, frame_stride)
    finally:
        emitter.close()
    log.info("done; events written to %s", emitter.path)


def _cli() -> None:
    ap = argparse.ArgumentParser(description="Run detection on one or more clips")
    ap.add_argument("--store-id", required=True)
    ap.add_argument("--layout", required=True)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--video", help="Path to a single mp4 clip")
    src.add_argument("--clips-dir", help="Directory containing all mp4 clips")
    ap.add_argument("--out-dir", default="data/events")
    ap.add_argument("--weights", default="yolov8n.pt")
    ap.add_argument("--frame-stride", type=int, default=1,
                    help="Run detection every Nth frame (1 = every frame)")
    args = ap.parse_args()
    run(args.store_id, args.layout, args.out_dir,
        clips_dir=args.clips_dir, video=args.video,
        weights=args.weights, frame_stride=args.frame_stride)


if __name__ == "__main__":
    _cli()
