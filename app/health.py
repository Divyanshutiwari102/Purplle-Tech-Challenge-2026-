"""
/health: status of the API for an on-call engineer.

Returns enough to answer 'is it up, is data flowing, is the DB OK'
without needing to ssh into the box.
"""
from __future__ import annotations

import time
from typing import Dict

from .db import OperationalError, get_connection
from .metrics import last_event_per_store, stale_feeds

_BOOT_TS = time.time()


def health(db_path: str | None = None) -> Dict:
    db_status = "ok"
    last_events: Dict[str, str] = {}
    stale: list[str] = []
    try:
        conn = get_connection(db_path)
        conn.execute("SELECT 1").fetchone()
        last_events = last_event_per_store(db_path)
        stale = stale_feeds(10, db_path)
    except OperationalError as e:
        db_status = f"error: {e}"

    status = "ok"
    if db_status != "ok":
        status = "degraded"
    elif stale:
        status = "warning"

    return {
        "status": status,
        "db_status": db_status,
        "last_event_per_store": last_events,
        "stale_feeds": stale,
        "uptime_seconds": int(time.time() - _BOOT_TS),
    }
