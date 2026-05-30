"""
Event emission helpers.

The detection layer constructs dicts that match the Pydantic schema in
app/models.py and writes them to a JSONL file the API can ingest.
Keeping emission separate from detection means the same writer can be
fed by tests and replays.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

EVENT_TYPES = {
    "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
    "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY",
}


def make_event(
    *,
    store_id: str,
    camera_id: str,
    visitor_id: str,
    event_type: str,
    timestamp: datetime,
    zone_id: Optional[str] = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float = 1.0,
    queue_depth: Optional[int] = None,
    sku_zone: Optional[str] = None,
    session_seq: int = 0,
) -> Dict[str, Any]:
    """Build an event dict that round-trips cleanly through Pydantic."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event_type: {event_type}")
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "zone_id": zone_id if event_type not in ("ENTRY", "EXIT") else None,
        "dwell_ms": int(dwell_ms),
        "is_staff": bool(is_staff),
        "confidence": float(max(0.0, min(1.0, confidence))),
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": sku_zone,
            "session_seq": int(session_seq),
        },
    }


class JSONLEmitter:
    """Append events to a per-store JSONL file."""

    def __init__(self, out_dir: str | Path = "data/events", store_id: str = "STORE"):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = self.out_dir / f"{store_id}_{ts}.jsonl"
        self._fh = open(self.path, "a", encoding="utf-8")

    def emit(self, event: Dict[str, Any]) -> None:
        self._fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    def __enter__(self) -> "JSONLEmitter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
