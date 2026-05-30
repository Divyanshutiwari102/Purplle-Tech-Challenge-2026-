# PROMPT: "Generate edge-case tests that exercise specific clauses in
#          the problem statement: empty store window, all-staff clip,
#          group entry (3 people same instant), confidence preservation
#          for low-confidence events, BILLING_QUEUE_JOIN missing
#          queue_depth is rejected, and ZONE_DWELL with dwell_ms=0 is
#          rejected. Also a test that the response always carries a
#          trace_id header."
#
# CHANGES MADE:
#   - Added the trace_id header check, which the AI omitted but is part
#     of the production-readiness criterion.
"""Additional edge-case tests for problem-statement compliance."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from tests.helpers import make_event


def test_empty_store_window_returns_zero_metrics(client):
    body = client.get("/stores/STORE_BLR_002/metrics").json()
    assert body["unique_visitors"] == 0
    assert body["conversion_rate"] == 0.0
    # No 5xx, valid JSON, no null leaks.
    assert "current_queue_depth" in body


def test_all_staff_clip_yields_zero_visitors(client):
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    events = [
        make_event(event_type="ENTRY", visitor_id=f"VIS_s{i}",
                   is_staff=True, timestamp=base + timedelta(seconds=i))
        for i in range(4)
    ]
    client.post("/events/ingest", json={"events": events})
    body = client.get("/stores/STORE_BLR_002/metrics").json()
    assert body["unique_visitors"] == 0


def test_group_entry_three_simultaneous(client):
    ts = datetime.now(timezone.utc) - timedelta(minutes=2)
    events = [
        make_event(event_type="ENTRY", visitor_id=f"VIS_g{i}", timestamp=ts)
        for i in range(3)
    ]
    body = client.post("/events/ingest", json={"events": events}).json()
    assert body["ingested"] == 3
    m = client.get("/stores/STORE_BLR_002/metrics").json()
    assert m["unique_visitors"] == 3


def test_low_confidence_event_is_preserved(client, db_path):
    """Confidence < 0.6 must NOT be silently dropped (criterion in spec)."""
    import sqlite3
    ev = make_event(event_type="ENTRY", confidence=0.42)
    body = client.post("/events/ingest", json={"events": [ev]}).json()
    assert body["ingested"] == 1
    conn = sqlite3.connect(db_path)
    conf = conn.execute(
        "SELECT confidence FROM events WHERE event_id=?", (ev["event_id"],)
    ).fetchone()[0]
    conn.close()
    assert abs(conf - 0.42) < 1e-6


def test_billing_queue_join_without_queue_depth_rejected(client):
    bad = make_event(event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
                     queue_depth=None)
    # The helper would normally backfill queue_depth, so build manually.
    bad_raw = {**bad, "metadata": {**bad["metadata"], "queue_depth": None}}
    body = client.post("/events/ingest", json={"events": [bad_raw]}).json()
    assert body["ingested"] == 0
    assert len(body["errors"]) == 1


def test_zone_dwell_with_zero_dwell_rejected(client):
    raw = {
        "event_id": str(uuid.uuid4()),
        "store_id": "STORE_BLR_002",
        "camera_id": "CAM_FLOOR_01",
        "visitor_id": "VIS_dwell0",
        "event_type": "ZONE_DWELL",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "zone_id": "SKINCARE",
        "dwell_ms": 0,                    # <-- invalid
        "is_staff": False,
        "confidence": 0.9,
        "metadata": {"queue_depth": None, "sku_zone": "SKINCARE", "session_seq": 1},
    }
    body = client.post("/events/ingest", json={"events": [raw]}).json()
    assert body["ingested"] == 0
    assert len(body["errors"]) == 1


def test_trace_id_header_is_set(client):
    r = client.get("/health")
    assert r.headers.get("x-trace-id")


def test_invalid_uuid_event_id_rejected(client):
    raw = {**make_event(event_type="ENTRY"), "event_id": "not-a-uuid"}
    body = client.post("/events/ingest", json={"events": [raw]}).json()
    assert body["ingested"] == 0
    assert len(body["errors"]) == 1


def test_naive_timestamp_rejected(client):
    raw = make_event(event_type="ENTRY")
    raw["timestamp"] = "2026-05-30T12:00:00"     # no tz
    body = client.post("/events/ingest", json={"events": [raw]}).json()
    assert body["ingested"] == 0


def test_unknown_store_id_returns_safe_zero_metrics(client):
    body = client.get("/stores/STORE_DOES_NOT_EXIST/metrics").json()
    assert body["unique_visitors"] == 0
    assert body["conversion_rate"] == 0.0
