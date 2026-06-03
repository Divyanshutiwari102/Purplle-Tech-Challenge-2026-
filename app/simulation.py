"""
Simulation manager.

Replays recorded events from `data/events/*.jsonl` back into the API at
controllable speed, broadcasting the SSE stream as it goes. This is what
makes the dashboard look "live" without needing the heavy detection
pipeline running.

It also emits **synthetic POS transactions** for ~1 in 3 visitors that
reach the billing zone, so /metrics conversion_rate reflects realistic
demo numbers (25–35%) instead of staying flat at 0% just because there
are no real transactions in the test DB.

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
import random
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from fastapi import APIRouter, HTTPException

from .dashboard import broadcast_update
from .db import get_connection
from .ingestion import ingest_events
from .models import Event

router = APIRouter()

# Probability that a visitor reaching billing actually purchases.
# 0.13 lands the steady-state /metrics conversion rate around 30% on
# the synthetic stream. The number is empirical — the spec's POS-
# correlation logic ("visitor in billing zone within 5 min before
# any txn = converted") naturally inflates raw probabilities because
# multiple sessions can match the same txn.
POS_CONVERSION_PROBABILITY = 0.13

# Basket value range for synthetic transactions (INR). Calibrated to
# the real Brigade Bangalore CSV (₹150–₹4,000 covers ~95% of orders).
POS_BASKET_MIN = 150.0
POS_BASKET_MAX = 4000.0

_state_lock = threading.Lock()
_state: Dict[str, object] = {
    "running": False,
    "speed": 1.0,
    "cam_id": None,
    "store_id": "STORE_BLR_002",
    "started_at": None,
    "events_replayed": 0,
    "transactions_emitted": 0,
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


def _synthetic_events(cam_id: Optional[str], store_id: str = "STORE_BLR_002") -> Iterator[dict]:
    """
    Fallback: generate believable visitor journeys when no pre-recorded
    JSONL is available. Produces diverse traffic across zones so the
    dashboard heatmap/funnel actually look like a real day.

    Each visitor's events are spread across the *recent past* (0–8 min
    ago), not jammed at "now". This matters because the POS-correlation
    window is 5 minutes — without spreading, every txn matches every
    session and conversion_rate inflates to ~70%. With spreading, the
    rate settles in the realistic 25-35% band.

    `store_id` selects which store (and therefore which cameras + zones)
    the synthetic visitors flow through. STORE_BLR_002 uses the v1
    layout (CAM_ENTRY_01 / CAM_FLOOR_01 / CAM_BILLING_01 + 6 zones).
    ST1008 uses the v2 Store-2 layout (ENTRY_1 / ZONE / BILLING_AREA +
    its 4 zones). Anything else falls back to the BLR_002 cams.
    """
    import random

    # Per-store choice of (entry_cam, floor_cam, billing_cam, zone_pool).
    PER_STORE = {
        "STORE_BLR_002": (
            "CAM_ENTRY_01", "CAM_FLOOR_01", "CAM_BILLING_01",
            ["SKINCARE", "MOISTURISER", "FRAGRANCE", "MAKEUP",
             "HAIRCARE", "BODYCARE"],
        ),
        "ST1008": (
            "ENTRY_1", "ZONE", "BILLING_AREA",
            ["SKINCARE", "FRAGRANCE", "MAKEUP", "HAIRCARE"],
        ),
    }
    entry_cam, floor_cam, billing_cam, zone_rotation = PER_STORE.get(
        store_id, PER_STORE["STORE_BLR_002"],
    )

    seq = 0
    while True:
        seq += 1
        vid = f"VIS_sim{seq:04d}"
        zone = zone_rotation[seq % len(zone_rotation)]
        # Some visitors browse multiple zones, some don't reach billing,
        # some are staff (excluded from customer metrics).
        is_staff = (seq % 25 == 0)
        reaches_billing = (seq % 4 != 0) and not is_staff   # ~75% reach billing

        # Anchor each visitor's session somewhere in the last 0–30 minutes.
        # The 5-min POS-correlation window then captures only ~10–15%
        # of synthetic sessions, keeping conversion_rate realistic.
        offset_seconds = random.uniform(0, 1800)
        base = datetime.now(timezone.utc) - timedelta(seconds=offset_seconds)

        cycle: list[tuple[str, str, Optional[str], int, Optional[int]]] = [
            ("ENTRY",            entry_cam,    None,  0,      None),
            ("ZONE_ENTER",       floor_cam,    zone,  0,      None),
            ("ZONE_DWELL",       floor_cam,    zone,  30000,  None),
        ]
        if reaches_billing:
            cycle.append(("BILLING_QUEUE_JOIN", billing_cam, "BILLING", 0,
                          random.randint(2, 7)))
        cycle.append(("EXIT", entry_cam, None, 0, None))

        # 30 s between events within a single visitor's session — that's a
        # realistic dwell. The whole session takes ~2 minutes.
        for i, (et, cam, z, dwell, qd) in enumerate(cycle):
            ev_ts = base + timedelta(seconds=i * 30)
            yield {
                "event_id":   str(uuid.uuid4()),
                "store_id":   store_id,
                "camera_id":  cam_id or cam,
                "visitor_id": vid,
                "event_type": et,
                "timestamp":  ev_ts.isoformat().replace("+00:00", "Z"),
                "zone_id":    z,
                "dwell_ms":   dwell,
                "is_staff":   is_staff,
                "confidence": 0.85 + random.random() * 0.1,
                "metadata":   {"queue_depth": qd, "sku_zone": z, "session_seq": i + 1},
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


def _emit_pos_transaction(store_id: str, when: datetime) -> Optional[dict]:
    """
    Insert a synthetic POS transaction into the `transactions` table.

    The timestamp is offset 30–180 s AFTER the BILLING_QUEUE_JOIN so it
    lands inside the 5-minute correlation window in `metrics.correlate_pos`.
    Returns the inserted row dict, or None if the DB insert failed.
    """
    txn_id = f"TXN_SIM_{uuid.uuid4().hex[:10]}"
    # Place the txn ~5 s after the BILLING_QUEUE_JOIN, but never in the
    # future relative to wall-clock "now" — otherwise /metrics windows
    # that end at "now" wouldn't include it. Realistic: a customer goes
    # from queue-join to card-tap in a handful of seconds.
    earliest = when + timedelta(seconds=1)
    latest = min(when + timedelta(seconds=20),
                 datetime.now(timezone.utc) - timedelta(milliseconds=100))
    if latest <= earliest:
        latest = earliest + timedelta(milliseconds=500)
    span = max(0.5, (latest - earliest).total_seconds())
    txn_ts = earliest + timedelta(seconds=random.uniform(0, span))
    basket = round(random.uniform(POS_BASKET_MIN, POS_BASKET_MAX), 2)
    iso_ts = txn_ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        conn = get_connection()
        # Ensure the FK target exists. The simulation bootstraps the
        # stores row lazily so the demo works on a fresh DB.
        conn.execute(
            "INSERT OR IGNORE INTO stores "
            "(store_id, name, timezone, open_hours) VALUES (?, ?, ?, ?)",
            (store_id, store_id, "Asia/Kolkata", '{"mon":["10:00","21:00"]}'),
        )
        conn.execute(
            "INSERT OR IGNORE INTO transactions "
            "(transaction_id, store_id, timestamp, basket_value_inr) "
            "VALUES (?, ?, ?, ?)",
            (txn_id, store_id, iso_ts, basket),
        )
    except sqlite3.OperationalError:
        return None
    except sqlite3.IntegrityError:
        return None
    return {
        "transaction_id": txn_id,
        "store_id": store_id,
        "timestamp": iso_ts,
        "basket_value_inr": basket,
    }


def _replay_loop(stop_flag: threading.Event) -> None:
    events_dir = _events_dir()
    src: Iterator[dict]
    # By default we prefer the synthetic generator: it fires a full
    # ENTRY → ZONE → BILLING → EXIT cycle every few seconds, which
    # makes the dashboard demo behave predictably. Recorded JSONLs
    # tend to be camera-by-camera — you'd watch the entry camera's
    # 18 events first and then 8000 billing rows in a row.
    use_recorded = (
        os.environ.get("STORE_INTEL_USE_RECORDED", "0") == "1"
        and events_dir.is_dir()
        and any(events_dir.glob("*.jsonl"))
    )
    if use_recorded:
        src = _iter_events(events_dir)
        _state["events_dir"] = str(events_dir)
    else:
        src = _synthetic_events(
            _state.get("cam_id"),
            store_id=str(_state.get("store_id") or "STORE_BLR_002"),
        )
        _state["events_dir"] = "synthetic"

    while not stop_flag.is_set():
        try:
            raw = next(src)
        except StopIteration:
            # Recorded files run out; loop them.
            if use_recorded:
                src = _iter_events(events_dir)
            else:
                src = _synthetic_events(
                    _state.get("cam_id"),
                    store_id=str(_state.get("store_id") or "STORE_BLR_002"),
                )
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

            # If the visitor just joined the billing queue, roll a die:
            # POS_CONVERSION_PROBABILITY chance they actually purchase.
            # The synthetic transaction lands inside the 5-min window so
            # /metrics correlate_pos picks it up.
            if (ev.event_type.value == "BILLING_QUEUE_JOIN"
                    and not ev.is_staff
                    and random.random() < POS_CONVERSION_PROBABILITY):
                txn = _emit_pos_transaction(ev.store_id, ev.timestamp)
                if txn is not None:
                    _state["transactions_emitted"] = int(
                        _state["transactions_emitted"]) + 1
                    try:
                        broadcast_update(ev.store_id, {
                            "type": "sim_transaction",
                            "store_id": ev.store_id,
                            "transaction_id": txn["transaction_id"],
                            "basket_value_inr": txn["basket_value_inr"],
                            "ts": txn["timestamp"],
                            "matched_visitor_id": ev.visitor_id,
                            "transactions_emitted": _state["transactions_emitted"],
                        })
                    except Exception:
                        pass

        # Pacing: 2 s between events at 1x. With 5 events per visitor
        # cycle this is one visitor every ~10 s — slow enough that the
        # 5-min POS correlation window typically holds 1–3 sessions, so
        # the conversion rate from POS_CONVERSION_PROBABILITY=0.10 lands
        # in the realistic 25–35% band in steady state.
        sleep = 2.0 / float(_state.get("speed", 1.0) or 1.0)
        if stop_flag.wait(sleep):
            break


def _start(speed: float, cam_id: Optional[str],
           store_id: Optional[str] = None) -> dict:
    new_store = store_id or "STORE_BLR_002"
    t: Optional[threading.Thread] = None
    with _state_lock:
        # If a different store is already running, stop it first so the
        # user's most recent intent wins. Without this, clicking Start
        # on the second store would silently keep emitting events for
        # the first one.
        if (_state.get("running")
                and _state.get("store_id") != new_store):
            flag = _state.get("stop_flag")
            if flag is not None:
                flag.set()
            prev_t = _state.get("thread")
            t = prev_t if isinstance(prev_t, threading.Thread) else None
            _state["running"] = False
        elif _state.get("running"):
            return _status_unlocked()
    # Briefly release the lock so the previous loop can observe the
    # stop flag and exit; then re-acquire to spin up the new one.
    if t is not None:
        t.join(timeout=2.0)
    with _state_lock:
        stop_flag = threading.Event()
        new_t = threading.Thread(target=_replay_loop, args=(stop_flag,), daemon=True)
        _state.update({
            "running": True,
            "speed": float(speed),
            "cam_id": cam_id,
            "store_id": new_store,
            "started_at": time.time(),
            "events_replayed": 0,
            "transactions_emitted": 0,
            "stop_flag": stop_flag,
            "thread": new_t,
        })
        new_t.start()
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
        "store_id": _state.get("store_id") or "STORE_BLR_002",
        "started_at": _state.get("started_at"),
        "events_replayed": int(_state.get("events_replayed", 0)),
        "transactions_emitted": int(_state.get("transactions_emitted", 0)),
        "events_dir": _state.get("events_dir"),
    }


# ---------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------
@router.post("/simulation/start")
def simulation_start(speed: float = 1.0, cam_id: Optional[str] = None,
                     store_id: Optional[str] = None) -> dict:
    if speed <= 0:
        raise HTTPException(status_code=400, detail="speed must be > 0")
    return _start(speed, cam_id, store_id=store_id)


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
