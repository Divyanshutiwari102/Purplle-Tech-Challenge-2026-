"""Test helpers — small builders for events and POS."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone


def make_event(
    *,
    event_type: str,
    store_id: str = "STORE_BLR_002",
    camera_id: str = "CAM_ENTRY_01",
    visitor_id: str | None = None,
    timestamp: datetime | None = None,
    zone_id: str | None = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float = 0.9,
    queue_depth: int | None = None,
    sku_zone: str | None = None,
    session_seq: int = 1,
) -> dict:
    if timestamp is None:
        # Default to 1 minute ago so the "today" window in /metrics
        # (which ends at "now") always includes the event.
        timestamp = datetime.now(timezone.utc) - timedelta(minutes=1)
    if visitor_id is None:
        visitor_id = f"VIS_{uuid.uuid4().hex[:6]}"

    # zone_id rules: null for threshold events (ENTRY / EXIT / REENTRY),
    # required for everything else.
    if event_type in ("ENTRY", "EXIT", "REENTRY"):
        zone_id = None
    elif zone_id is None:
        zone_id = "SKINCARE"

    if event_type == "ZONE_DWELL" and dwell_ms <= 0:
        dwell_ms = 30000
    if event_type == "BILLING_QUEUE_JOIN" and queue_depth is None:
        queue_depth = 4

    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": sku_zone,
            "session_seq": session_seq,
        },
    }
