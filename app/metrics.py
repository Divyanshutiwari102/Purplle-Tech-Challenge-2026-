"""
Real-time metrics + POS correlation.

All queries here are intentionally written in plain SQL so that what
runs in the database is exactly what the reviewer reads in the code.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from .db import get_connection


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _today_window(now: Optional[datetime] = None) -> Tuple[str, str]:
    """Start-of-day UTC to now."""
    now = now or _utcnow()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return _iso(start), _iso(now)


# ---------------------------------------------------------------------
# POS correlation: the heart of conversion_rate
# ---------------------------------------------------------------------
def correlate_pos(
    store_id: str,
    window_start: str,
    window_end: str,
    db_path: str | None = None,
) -> Tuple[int, int]:
    """
    Returns (converted_sessions, total_sessions) for the window.

    Logic (from the problem statement):
      A visitor session counts as converted if the visitor was in the
      billing zone within the 5 minutes BEFORE any POS transaction in
      the window. One conversion per session, even if multiple
      transactions match.

    Implementation:
      • Pull all transactions in [window_start, window_end].
      • For each, find sessions with a BILLING_QUEUE_JOIN or
        ZONE_ENTER(billing) event in [txn-5min, txn].
      • Mark those sessions converted; deduplicate by session_id.
      • total_sessions = unique non-staff sessions whose entry_time
        falls in the window.
    """
    conn = get_connection(db_path)

    txns = conn.execute(
        "SELECT transaction_id, timestamp FROM transactions "
        "WHERE store_id = ? AND timestamp BETWEEN ? AND ? "
        "ORDER BY timestamp",
        (store_id, window_start, window_end),
    ).fetchall()

    # Total non-staff sessions in window.
    total = conn.execute(
        "SELECT COUNT(*) FROM visitor_sessions "
        "WHERE store_id = ? AND is_staff = 0 "
        "  AND entry_time BETWEEN ? AND ?",
        (store_id, window_start, window_end),
    ).fetchone()[0]

    if not txns or total == 0:
        return 0, total

    converted_sessions: set[str] = set()
    for txn in txns:
        txn_ts = datetime.fromisoformat(txn["timestamp"].replace("Z", "+00:00"))
        five_min_before = _iso(txn_ts - timedelta(minutes=5))
        rows = conn.execute(
            """
            SELECT DISTINCT s.session_id
            FROM visitor_sessions s
            JOIN events e
              ON e.visitor_id = s.visitor_id
             AND e.store_id  = s.store_id
            WHERE s.store_id = ?
              AND s.is_staff = 0
              AND e.timestamp BETWEEN ? AND ?
              AND (
                   e.event_type = 'BILLING_QUEUE_JOIN'
                OR (e.event_type IN ('ZONE_ENTER','ZONE_DWELL')
                    AND e.zone_id LIKE 'BILLING%')
              )
            """,
            (store_id, five_min_before, txn["timestamp"]),
        ).fetchall()
        converted_sessions.update(r["session_id"] for r in rows)

    return len(converted_sessions), total


# ---------------------------------------------------------------------
# /metrics
# ---------------------------------------------------------------------
def store_metrics(store_id: str, db_path: str | None = None) -> Dict:
    conn = get_connection(db_path)
    start, end = _today_window()

    unique_visitors = conn.execute(
        "SELECT COUNT(DISTINCT visitor_id) FROM events "
        "WHERE store_id = ? AND is_staff = 0 "
        "  AND event_type = 'ENTRY' "
        "  AND timestamp BETWEEN ? AND ?",
        (store_id, start, end),
    ).fetchone()[0]

    converted, total = correlate_pos(store_id, start, end, db_path)
    conversion_rate = (converted / total) if total > 0 else 0.0

    avg_dwell_rows = conn.execute(
        """
        SELECT zone_id, AVG(dwell_ms) AS avg_dwell
        FROM events
        WHERE store_id = ? AND is_staff = 0
          AND event_type = 'ZONE_DWELL'
          AND zone_id IS NOT NULL
          AND timestamp BETWEEN ? AND ?
        GROUP BY zone_id
        """,
        (store_id, start, end),
    ).fetchall()
    avg_dwell_per_zone = {r["zone_id"]: float(r["avg_dwell"] or 0) for r in avg_dwell_rows}

    # Current queue depth: latest BILLING_QUEUE_JOIN's queue_depth in last 5 min.
    five_min_ago = _iso(_utcnow() - timedelta(minutes=5))
    qrow = conn.execute(
        """
        SELECT metadata FROM events
        WHERE store_id = ? AND event_type = 'BILLING_QUEUE_JOIN'
          AND timestamp >= ?
        ORDER BY timestamp DESC LIMIT 1
        """,
        (store_id, five_min_ago),
    ).fetchone()
    current_queue_depth = 0
    if qrow:
        try:
            current_queue_depth = int(json.loads(qrow["metadata"]).get("queue_depth") or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            current_queue_depth = 0

    # Abandonment rate: ABANDON / (JOIN) today.
    joins = conn.execute(
        "SELECT COUNT(*) FROM events WHERE store_id = ? "
        "AND event_type = 'BILLING_QUEUE_JOIN' AND timestamp BETWEEN ? AND ?",
        (store_id, start, end),
    ).fetchone()[0]
    abandons = conn.execute(
        "SELECT COUNT(*) FROM events WHERE store_id = ? "
        "AND event_type = 'BILLING_QUEUE_ABANDON' AND timestamp BETWEEN ? AND ?",
        (store_id, start, end),
    ).fetchone()[0]
    abandonment_rate = (abandons / joins) if joins > 0 else 0.0

    return {
        "store_id": store_id,
        "window_start": start,
        "window_end": end,
        "unique_visitors": int(unique_visitors),
        "conversion_rate": round(conversion_rate, 4),
        "avg_dwell_per_zone": avg_dwell_per_zone,
        "current_queue_depth": current_queue_depth,
        "abandonment_rate": round(abandonment_rate, 4),
        "converted_sessions": converted,
        "total_sessions": total,
    }


# ---------------------------------------------------------------------
# /funnel
# ---------------------------------------------------------------------
def store_funnel(store_id: str, db_path: str | None = None) -> Dict:
    """
    Funnel by SESSION (not raw events). Re-entries collapse to the same
    visitor_id, so DISTINCT visitor_id gives us the unique-visitor count.
    """
    conn = get_connection(db_path)
    start, end = _today_window()

    def _distinct_visitors(where_clause: str, params: tuple) -> int:
        return conn.execute(
            f"SELECT COUNT(DISTINCT visitor_id) FROM events "
            f"WHERE store_id = ? AND is_staff = 0 "
            f"  AND timestamp BETWEEN ? AND ? AND {where_clause}",
            (store_id, start, end, *params),
        ).fetchone()[0]

    entry_count = _distinct_visitors("event_type IN ('ENTRY','REENTRY')", ())
    zone_visit_count = _distinct_visitors(
        "event_type = 'ZONE_ENTER' AND zone_id NOT LIKE 'BILLING%'", ()
    )
    billing_queue_count = _distinct_visitors(
        "event_type IN ('BILLING_QUEUE_JOIN','ZONE_ENTER') "
        "AND zone_id LIKE 'BILLING%'", ()
    )

    converted, _total = correlate_pos(store_id, start, end, db_path)
    purchase_count = converted

    def _drop(a: int, b: int) -> float:
        return round((1.0 - (b / a)) * 100.0, 2) if a > 0 else 0.0

    return {
        "store_id": store_id,
        "entry_count": entry_count,
        "zone_visit_count": zone_visit_count,
        "billing_queue_count": billing_queue_count,
        "purchase_count": purchase_count,
        "entry_to_zone_dropoff_pct": _drop(entry_count, zone_visit_count),
        "zone_to_billing_dropoff_pct": _drop(zone_visit_count, billing_queue_count),
        "billing_to_purchase_dropoff_pct": _drop(billing_queue_count, purchase_count),
    }


# ---------------------------------------------------------------------
# /heatmap
# ---------------------------------------------------------------------
def store_heatmap(store_id: str, db_path: str | None = None) -> List[Dict]:
    conn = get_connection(db_path)
    start, end = _today_window()

    rows = conn.execute(
        """
        SELECT zone_id,
               COUNT(*) AS visits,
               AVG(dwell_ms) AS avg_dwell,
               COUNT(DISTINCT visitor_id) AS sessions
        FROM events
        WHERE store_id = ? AND is_staff = 0
          AND event_type IN ('ZONE_ENTER','ZONE_DWELL')
          AND zone_id IS NOT NULL
          AND timestamp BETWEEN ? AND ?
        GROUP BY zone_id
        """,
        (store_id, start, end),
    ).fetchall()

    if not rows:
        return []

    max_visits = max(r["visits"] for r in rows) or 1
    return [
        {
            "zone_id": r["zone_id"],
            "visit_frequency": int(r["visits"]),
            "avg_dwell_ms": float(r["avg_dwell"] or 0),
            "normalized_score": round((r["visits"] / max_visits) * 100.0, 2),
            "data_confidence": int(r["sessions"]) >= 20,
            "session_count": int(r["sessions"]),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------
# Helpers used by /health and /anomalies
# ---------------------------------------------------------------------
def last_event_per_store(db_path: str | None = None) -> Dict[str, str]:
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT store_id, MAX(timestamp) AS last_ts FROM events GROUP BY store_id"
    ).fetchall()
    return {r["store_id"]: r["last_ts"] for r in rows}


def stale_feeds(threshold_minutes: int = 10, db_path: str | None = None) -> List[str]:
    cutoff = _iso(_utcnow() - timedelta(minutes=threshold_minutes))
    return [
        store_id
        for store_id, last_ts in last_event_per_store(db_path).items()
        if last_ts < cutoff
    ]
