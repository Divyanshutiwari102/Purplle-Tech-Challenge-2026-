# PROMPT: "Generate pytest tests for a /metrics, /funnel, /heatmap REST
#          API. Cover: zero-purchase store must return conversion_rate
#          0.0 (not null, not error), funnel with re-entries must count
#          a visitor once, heatmap with <20 sessions must set
#          data_confidence false, duplicate event ingestion must be
#          idempotent (second post returns duplicates count, not 5xx).
#          Use FastAPI TestClient. Build events through a helper."
#
# CHANGES MADE:
#   - Added explicit POS-correlated session: a visitor in BILLING zone
#     within 5 minutes before a transaction → conversion=1. This is the
#     business rule and the AI version assumed conversion was a stored
#     boolean.
#   - Asserted on the structured response (rounded floats) instead of
#     stringified JSON because Pydantic floats can otherwise vary.
"""Tests for the metrics / funnel / heatmap endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.helpers import make_event


# ---------------------------------------------------------------------
def test_zero_purchases_returns_conversion_rate_zero(client):
    # Use timestamps in the recent past so the "today" window covers them.
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    # 3 visitors, no transactions logged.
    for i in range(3):
        ev = make_event(event_type="ENTRY", visitor_id=f"VIS_{i}",
                        timestamp=base + timedelta(seconds=i))
        client.post("/events/ingest", json={"events": [ev]})

    m = client.get("/stores/STORE_BLR_002/metrics").json()
    assert m["conversion_rate"] == 0.0
    assert m["unique_visitors"] == 3


# ---------------------------------------------------------------------
def test_funnel_with_reentry_counts_visitor_once(client):
    base = datetime.now(timezone.utc) - timedelta(minutes=20)
    vid = "VIS_loop"
    events = [
        make_event(event_type="ENTRY", visitor_id=vid, timestamp=base),
        make_event(event_type="ZONE_ENTER", visitor_id=vid, zone_id="SKINCARE",
                   timestamp=base + timedelta(seconds=10)),
        make_event(event_type="EXIT", visitor_id=vid, timestamp=base + timedelta(minutes=4)),
        make_event(event_type="REENTRY", visitor_id=vid, timestamp=base + timedelta(minutes=8)),
        make_event(event_type="ZONE_ENTER", visitor_id=vid, zone_id="SKINCARE",
                   timestamp=base + timedelta(minutes=9)),
    ]
    client.post("/events/ingest", json={"events": events})

    funnel = client.get("/stores/STORE_BLR_002/funnel").json()
    assert funnel["entry_count"] == 1
    assert funnel["zone_visit_count"] == 1


# ---------------------------------------------------------------------
def test_heatmap_data_confidence_false_when_few_sessions(client):
    base = datetime.now(timezone.utc)
    events = [
        make_event(event_type="ZONE_ENTER", visitor_id=f"VIS_{i}",
                   zone_id="SKINCARE", timestamp=base + timedelta(seconds=i))
        for i in range(5)  # <20 sessions
    ]
    client.post("/events/ingest", json={"events": events})

    body = client.get("/stores/STORE_BLR_002/heatmap").json()
    zones = body["zones"]
    assert any(z["zone_id"] == "SKINCARE" and z["data_confidence"] is False
               for z in zones)


# ---------------------------------------------------------------------
def test_duplicate_event_ingestion_is_idempotent(client):
    ev = make_event(event_type="ENTRY")
    r1 = client.post("/events/ingest", json={"events": [ev]})
    r2 = client.post("/events/ingest", json={"events": [ev]})
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["ingested"] == 1
    assert r2.json()["ingested"] == 0
    assert r2.json()["duplicates"] == 1


# ---------------------------------------------------------------------
def test_partial_failure_returns_structured_errors(client):
    # Build a bad event by hand to bypass helper's auto-fix logic.
    import uuid as _uuid
    good = make_event(event_type="ENTRY")
    bad = {
        "event_id": str(_uuid.uuid4()),
        "store_id": "STORE_BLR_002",
        "camera_id": "CAM_FLOOR_01",
        "visitor_id": "VIS_bad",
        "event_type": "ZONE_DWELL",
        "timestamp": good["timestamp"],
        "zone_id": "SKINCARE",
        "dwell_ms": 0,                  # invalid: must be > 0 for ZONE_DWELL
        "is_staff": False,
        "confidence": 0.9,
        "metadata": {"queue_depth": None, "sku_zone": "SKINCARE", "session_seq": 1},
    }
    body = client.post("/events/ingest", json={"events": [good, bad]}).json()
    assert body["ingested"] == 1
    assert len(body["errors"]) == 1


# ---------------------------------------------------------------------
def test_pos_correlation_marks_session_converted(client, db_path):
    # Insert a transaction, then a visitor in BILLING just before.
    import sqlite3, uuid
    # Anchor everything in the recent past so the "today" window catches it.
    base = datetime.now(timezone.utc) - timedelta(minutes=30)
    txn_ts = base + timedelta(minutes=10)

    vid = "VIS_buyer"
    events = [
        make_event(event_type="ENTRY", visitor_id=vid, timestamp=base),
        make_event(event_type="ZONE_ENTER", visitor_id=vid,
                   zone_id="BILLING", camera_id="CAM_BILLING_01",
                   timestamp=txn_ts - timedelta(minutes=2)),
        make_event(event_type="BILLING_QUEUE_JOIN", visitor_id=vid,
                   zone_id="BILLING", camera_id="CAM_BILLING_01",
                   timestamp=txn_ts - timedelta(minutes=1)),
    ]
    client.post("/events/ingest", json={"events": events})

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO transactions VALUES (?,?,?,?)",
        (str(uuid.uuid4()), "STORE_BLR_002",
         txn_ts.isoformat().replace("+00:00", "Z"), 1240.00),
    )
    conn.commit()
    conn.close()

    m = client.get("/stores/STORE_BLR_002/metrics").json()
    assert m["converted_sessions"] == 1
    assert m["conversion_rate"] == 1.0
