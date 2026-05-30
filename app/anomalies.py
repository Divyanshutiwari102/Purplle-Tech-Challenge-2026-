"""
Anomaly detection.

Each detector returns a list of anomaly dicts. They are pure functions
of the events table — recomputed on every /anomalies request rather than
cached, because the spec asks for *active* anomalies.

Severities:
  INFO     — informational (e.g. low queue forming)
  WARN     — needs attention soon (e.g. queue spike)
  CRITICAL — needs attention now (e.g. stale camera, conversion drop)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from .db import get_connection
from .metrics import _iso, _today_window, _utcnow, correlate_pos


def _ago(minutes: int) -> str:
    return _iso(_utcnow() - timedelta(minutes=minutes))


# ---------------------------------------------------------------------
def _queue_spike(store_id: str, db_path: Optional[str]) -> List[Dict]:
    conn = get_connection(db_path)
    # Latest queue_depth in the last 5 minutes.
    row = conn.execute(
        """
        SELECT metadata, timestamp FROM events
        WHERE store_id = ? AND event_type = 'BILLING_QUEUE_JOIN'
          AND timestamp >= ?
        ORDER BY timestamp DESC LIMIT 1
        """,
        (store_id, _ago(5)),
    ).fetchone()
    if not row:
        return []
    try:
        depth = int(json.loads(row["metadata"]).get("queue_depth") or 0)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []

    # Average queue depth over the last hour for comparison.
    avg_row = conn.execute(
        """
        SELECT AVG(json_extract(metadata,'$.queue_depth')) AS avg_depth
        FROM events
        WHERE store_id = ? AND event_type = 'BILLING_QUEUE_JOIN'
          AND timestamp >= ?
        """,
        (store_id, _ago(60)),
    ).fetchone()
    avg_depth = float(avg_row["avg_depth"] or 0)

    if depth > 5 or (avg_depth > 0 and depth > 2 * avg_depth):
        severity = "CRITICAL" if depth > 8 else "WARN"
        return [{
            "type": "QUEUE_SPIKE",
            "severity": severity,
            "description": f"Billing queue depth is {depth} (1h avg: {avg_depth:.1f}).",
            "suggested_action": "Open an additional billing counter or redirect staff.",
        }]
    return []


# ---------------------------------------------------------------------
def _conversion_drop(store_id: str, db_path: Optional[str]) -> List[Dict]:
    today_start, now = _today_window()
    today_conv, today_total = correlate_pos(store_id, today_start, now, db_path)
    if today_total < 10:
        return []  # too little data — don't fire false positives
    today_rate = today_conv / today_total

    week_start = _iso(_utcnow() - timedelta(days=7))
    week_conv, week_total = correlate_pos(store_id, week_start, today_start, db_path)
    if week_total == 0:
        return []
    week_rate = week_conv / week_total

    if week_rate > 0 and today_rate < 0.7 * week_rate:
        return [{
            "type": "CONVERSION_DROP",
            "severity": "CRITICAL",
            "description": (
                f"Today's conversion {today_rate:.1%} is below 70% of "
                f"7-day average {week_rate:.1%}."
            ),
            "suggested_action": "Investigate floor staffing and billing throughput.",
        }]
    return []


# ---------------------------------------------------------------------
def _dead_zone(store_id: str, db_path: Optional[str]) -> List[Dict]:
    conn = get_connection(db_path)
    zones = conn.execute(
        "SELECT zone_id FROM zones WHERE store_id = ?", (store_id,)
    ).fetchall()
    if not zones:
        # Fall back to zones we've seen in events.
        zones = conn.execute(
            "SELECT DISTINCT zone_id AS zone_id FROM events "
            "WHERE store_id = ? AND zone_id IS NOT NULL",
            (store_id,),
        ).fetchall()

    cutoff = _ago(30)
    out: List[Dict] = []
    for z in zones:
        zone_id = z["zone_id"]
        if not zone_id or zone_id.startswith("BILLING"):
            continue
        hit = conn.execute(
            "SELECT 1 FROM events WHERE store_id = ? AND zone_id = ? "
            "AND event_type = 'ZONE_ENTER' AND timestamp >= ? LIMIT 1",
            (store_id, zone_id, cutoff),
        ).fetchone()
        if not hit:
            out.append({
                "type": "DEAD_ZONE",
                "severity": "WARN",
                "description": f"Zone {zone_id} has had no visits in the past 30 minutes.",
                "suggested_action": f"Check merchandising / staff coverage for {zone_id}.",
            })
    return out


# ---------------------------------------------------------------------
def _stale_camera(store_id: str, db_path: Optional[str]) -> List[Dict]:
    conn = get_connection(db_path)
    cutoff = _ago(10)
    rows = conn.execute(
        """
        SELECT camera_id, MAX(timestamp) AS last_ts
        FROM events WHERE store_id = ?
        GROUP BY camera_id
        """,
        (store_id,),
    ).fetchall()
    out: List[Dict] = []
    for r in rows:
        if r["last_ts"] < cutoff:
            out.append({
                "type": "STALE_CAMERA",
                "severity": "CRITICAL",
                "description": f"Camera {r['camera_id']} has not produced events since {r['last_ts']}.",
                "suggested_action": f"Check connectivity for {r['camera_id']}; restart the edge agent.",
            })
    return out


# ---------------------------------------------------------------------
def detect_active(store_id: str, db_path: str | None = None) -> List[Dict]:
    return [
        *_queue_spike(store_id, db_path),
        *_conversion_drop(store_id, db_path),
        *_dead_zone(store_id, db_path),
        *_stale_camera(store_id, db_path),
    ]
