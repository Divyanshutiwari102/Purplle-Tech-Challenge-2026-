"""
Simulation manager.

Replays recorded events from `data/events/*.jsonl` back into the API at
controllable speed, broadcasting the SSE stream as it goes. This is what
makes the dashboard look "live" without needing the heavy detection
pipeline running.

Endpoints:
  POST /simulation/start?speed=1.0&cam_id=CAM_1
  POST /simulation/stop
  POST /simulation/speed?speed=2.0
  GET  /simulation/status
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from fastapi import APIRouter, HTTPException

from .dashboard import broadcast_update
from .ingestion import ingest_events
from .models import Event

router = APIRouter()

_state_lock = threading.Lock()
_state: Dict[str, object] = {
    "running": False,
    "speed": 1.0,
    "cam_id": None,
    "started_at": None,
    "events_replayed": 0,
    "thread": None,
    "stop_flag": None,
    "events_dir": None,
}


def _events_dir() -> Path:
    """Find a directory of pre-recorded JSONL events."""
    for p in [
        os.environ.get("STORE_INTEL_EVENTS_DIR", ""),
        "/data/events",
        "data/events",
    ]:
        if p and Path(p).is_dir() and any(Path(p).glob("*.jsonl")):
            return Path(p)
    # Fall back to creating a synthetic stream below.
    return Path("data/events")


def _iter_events(events_dir: Path) -> Iterator[dict]:
    for f in sorted(events_dir.glob("*.jsonl")):
        with open(f, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue


def _synthetic_events(cam_id: Optional[str]) -> Iterator[dict]:
    """
    Fallback: generate a believable visitor journey when no pre-recorded
    JSONL is available. One synthetic visitor every 4 seconds, with a
    full ENTRY → ZONE → BILLING → EXIT lifecycle.
    """
    seq = 0
    while True:
        seq += 1
        vid = f"VIS_sim{seq:03d}"
        now = datetime.now(timezone.utc)
        cycle = [
            ("ENTRY",            "CAM_ENTRY_01",   None,         0,   None),
            ("ZONE_ENTER",       "CAM_FLOOR_01",   "SKINCARE",   0,   None),
            ("ZONE_DWELL",       "CAM_FLOOR_01",   "SKINCARE",   30000, None),
            ("BILLING_QUEUE_JOIN","CAM_BILLING_01","BILLING",    0,   3),
            ("EXIT",             "CAM_ENTRY_01",   None,         0,   None),
        ]
        for i, (et, cam, zone, dwell, qd) in enumerate(cycle):
            yield {
                "event_id":   str(uuid.uuid4()),
                "store_id":   "STORE_BLR_002",
                "camera_id":  cam_id or cam,
                "visitor_id": vid,
                "event_type": et,
                "timestamp":  (now + timedelta(seconds=i)).isoformat().replace("+00:00", "Z"),
                "zone_id":    zone,
                "dwell_ms":   dwell,
                "is_staff":   False,
                "confidence": 0.9,
                "metadata":   {"queue_depth": qd, "sku_zone": zone, "session_seq": i + 1},
            }


def _stamp_now(raw: dict) -> dict:
    """Rewrite the recorded timestamp to "now" so the API treats it as live."""
    new = dict(raw)
    new["event_id"] = str(uuid.uuid4())
    new["timestamp"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return new


def _filter_cam(ev: dict, cam_id: Optional[str]) -> bool:
    if cam_id is None:
        return True
    return ev.get("camera_id") == cam_id


def _replay_loop(stop_flag: threading.Event) -> None:
    events_dir = _events_dir()
    src: Iterator[dict]
    if events_dir.is_dir() and any(events_dir.glob("*.jsonl")):
        src = _iter_events(events_dir)
        _state["events_dir"] = str(events_dir)
    else:
        src = _synthetic_events(_state.get("cam_id"))
        _state["events_dir"] = "synthetic"

    while not stop_flag.is_set():
        try:
            raw = next(src)
        except StopIteration:
            # Loop the recorded file forever (simulation = always live).
            src = _iter_events(events_dir) if events_dir.is_dir() else _synthetic_events(_state.get("cam_id"))
            continue

        if not _filter_cam(raw, _state.get("cam_id")):
            continue

        ev_dict = _stamp_now(raw)
        try:
            ev = Event.model_validate(ev_dict)
        except Exception:
            continue

        ingested, _, _ = ingest_events([ev])
        if ingested:
            _state["events_replayed"] = int(_state["events_replayed"]) + 1
            try:
                broadcast_update(ev.store_id, {
                    "type": "sim_event",
                    "store_id": ev.store_id,
                    "event_type": ev.event_type.value,
                    "visitor_id": ev.visitor_id,
                    "camera_id": ev.camera_id,
                    "zone_id": ev.zone_id,
                    "ts": ev.timestamp.isoformat().replace("+00:00", "Z"),
                    "events_replayed": _state["events_replayed"],
                })
            except Exception:
                pass

        sleep = 0.5 / float(_state.get("speed", 1.0) or 1.0)
        if stop_flag.wait(sleep):
            break


def _start(speed: float, cam_id: Optional[str]) -> dict:
    with _state_lock:
        if _state.get("running"):
            return _status_unlocked()
        stop_flag = threading.Event()
        t = threading.Thread(target=_replay_loop, args=(stop_flag,), daemon=True)
        _state.update({
            "running": True,
            "speed": float(speed),
            "cam_id": cam_id,
            "started_at": time.time(),
            "events_replayed": 0,
            "stop_flag": stop_flag,
            "thread": t,
        })
        t.start()
        return _status_unlocked()


def _stop() -> dict:
    with _state_lock:
        if _state.get("running"):
            flag = _state.get("stop_flag")
            if flag is not None:
                flag.set()
            _state["running"] = False
        return _status_unlocked()


def _set_speed(speed: float) -> dict:
    with _state_lock:
        _state["speed"] = float(speed)
        return _status_unlocked()


def _status_unlocked() -> dict:
    return {
        "running": bool(_state.get("running")),
        "speed": float(_state.get("speed", 1.0) or 1.0),
        "cam_id": _state.get("cam_id"),
        "started_at": _state.get("started_at"),
        "events_replayed": int(_state.get("events_replayed", 0)),
        "events_dir": _state.get("events_dir"),
    }


# ---------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------
@router.post("/simulation/start")
def simulation_start(speed: float = 1.0, cam_id: Optional[str] = None) -> dict:
    if speed <= 0:
        raise HTTPException(status_code=400, detail="speed must be > 0")
    return _start(speed, cam_id)


@router.post("/simulation/stop")
def simulation_stop() -> dict:
    return _stop()


@router.post("/simulation/speed")
def simulation_speed(speed: float = 1.0) -> dict:
    if speed <= 0:
        raise HTTPException(status_code=400, detail="speed must be > 0")
    return _set_speed(speed)


@router.get("/simulation/status")
def simulation_status() -> dict:
    with _state_lock:
        return _status_unlocked()
