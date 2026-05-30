"""
Pre-compute YOLO detections for every clip and save as compact JSON.

Why pre-compute?
  - Live YOLO inference at 1080p on CPU costs ~80–120 ms per frame.
    The MJPEG stream wants ~10 fps, so live inference would saturate
    the CPU and starve the rest of the API.
  - For the demo, the clips are static. Detecting once and replaying
    is honest: the same model runs, the bboxes are real, and the
    stream stays smooth.

Output format (per clip):
  {
    "clip": "CAM 1.mp4",
    "camera_id": "CAM_1",
    "frame_size": [W, H],
    "fps": 30.0,
    "stride": 5,
    "frames": [
      {"i": 0, "boxes": [[x1, y1, x2, y2, conf, track_id], ...]},
      {"i": 5, "boxes": [...]},
      ...
    ]
  }

A simple IoU tracker assigns track_ids so the bbox colours stay stable
across frames — that's what makes the box "follow" the person.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

CONF = 0.30
DEFAULT_STRIDE = 5
IOU_MATCH = 0.3
TRACK_TIMEOUT = 30  # frames since last seen before a track is dropped


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


def _process_one(clip_path: Path, camera_id: str, weights: str,
                 stride: int, out_dir: Path) -> None:
    import cv2
    from ultralytics import YOLO

    model = YOLO(weights)
    cap = cv2.VideoCapture(str(clip_path))
    if not cap.isOpened():
        print(f"!! cannot open {clip_path}", file=sys.stderr)
        return
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    fh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)

    tracks: Dict[int, dict] = {}   # tid -> {bbox, missed}
    next_tid = 1
    frames_out: List[dict] = []

    frame_idx = 0
    last_log = time.time()
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_idx % stride != 0:
            frame_idx += 1
            continue

        results = model(frame, classes=[0], conf=CONF, verbose=False)
        det = []
        for r in results:
            for b in r.boxes:
                x1, y1, x2, y2 = [float(v) for v in b.xyxy[0].tolist()]
                c = float(b.conf[0])
                det.append([x1, y1, x2, y2, c])

        # Greedy IoU matching to existing tracks.
        unmatched = list(range(len(det)))
        matched: Dict[int, int] = {}     # tid -> det idx
        for tid, t in list(tracks.items()):
            best, best_j = 0.0, -1
            for j in unmatched:
                v = _iou(t["bbox"], det[j][:4])
                if v > best:
                    best, best_j = v, j
            if best >= IOU_MATCH and best_j >= 0:
                matched[tid] = best_j
                unmatched.remove(best_j)
                tracks[tid]["bbox"] = det[best_j][:4]
                tracks[tid]["missed"] = 0
        # New tracks for unmatched detections.
        for j in unmatched:
            tracks[next_tid] = {"bbox": det[j][:4], "missed": 0}
            matched[next_tid] = j
            next_tid += 1
        # Age and prune stale tracks.
        for tid in list(tracks.keys()):
            if tid not in matched:
                tracks[tid]["missed"] += 1
                if tracks[tid]["missed"] > TRACK_TIMEOUT:
                    del tracks[tid]

        boxes = []
        for tid, j in matched.items():
            x1, y1, x2, y2, c = det[j]
            boxes.append([round(x1, 1), round(y1, 1), round(x2, 1),
                          round(y2, 1), round(c, 3), int(tid)])
        if boxes:
            frames_out.append({"i": frame_idx, "boxes": boxes})

        frame_idx += 1
        if time.time() - last_log > 5:
            pct = (100.0 * frame_idx / n_frames) if n_frames else 0
            print(f"  {clip_path.name}  {frame_idx}/{n_frames} ({pct:.0f}%)")
            last_log = time.time()

    cap.release()

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{camera_id}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "clip": clip_path.name,
            "camera_id": camera_id,
            "frame_size": [fw, fh],
            "fps": fps,
            "stride": stride,
            "n_frames": n_frames,
            "frames": frames_out,
        }, f, separators=(",", ":"))
    print(f"  → {out_path}  ({len(frames_out)} populated frames)")


def _resolve_camera_id(name: str) -> str:
    layout_path = Path("data/layout/store_layout.json")
    if layout_path.exists():
        try:
            layout = json.loads(layout_path.read_text(encoding="utf-8"))
            stores = layout.get("stores", {})
            for s in stores.values():
                m = s.get("clip_to_camera", {}) or {}
                if name in m:
                    return m[name]
        except Exception:
            pass
    # Fallback: "CAM 1.mp4" → "CAM_1"
    stem = Path(name).stem.replace(" ", "_").upper()
    return stem


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips-dir", default="data/clips")
    ap.add_argument("--out-dir", default="data/detections")
    ap.add_argument("--weights", default="yolov8n.pt")
    ap.add_argument("--stride", type=int, default=DEFAULT_STRIDE)
    args = ap.parse_args()

    clips = sorted(Path(args.clips_dir).glob("*.mp4"))
    if not clips:
        print(f"no clips in {args.clips_dir}")
        sys.exit(2)

    out_dir = Path(args.out_dir)
    print(f"Pre-computing detections for {len(clips)} clips → {out_dir}")
    for c in clips:
        cam = _resolve_camera_id(c.name)
        # Normalise to CAM_N if layout exists.
        cam_short = {
            "CAM_ENTRY_01": "CAM_1",
            "CAM_FLOOR_01": "CAM_2",
            "CAM_FLOOR_02": "CAM_3",
            "CAM_BILLING_01": "CAM_4",
            "CAM_BILLING_02": "CAM_5",
        }.get(cam, cam)
        print(f"\n=== {c.name} → {cam_short} ===")
        _process_one(c, cam_short, args.weights, args.stride, out_dir)


if __name__ == "__main__":
    main()
