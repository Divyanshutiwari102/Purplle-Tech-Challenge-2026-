# PROMPT: "Generate pytest tests for an anomaly-detection endpoint that
#          returns active anomalies as a list of typed objects. Cover:
#          QUEUE_SPIKE when current depth > 5, DEAD_ZONE when a zone has
#          no visits in past 30 min, STALE_CAMERA when a camera produced
#          no events in past 10 min, and a /health endpoint returning
#          status, last_event_per_store dict, stale_feeds list, and
#          db_status. Use FastAPI TestClient."
#
# CHANGES MADE:
#   - Replaced AI's freezegun pattern with explicit timestamps in the
#     past, because freezegun is heavy and the code uses datetime.now()
#     in too many places to mock cleanly. The deterministic past-timestamp
#     approach is more honest about what the test exercises.
#   - Asserted on anomaly *types* present in the response, not on order,
#     because the detector composition order is an implementation detail.
"""Tests for /anomalies and /health."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.helpers import make_event


def test_queue_spike_is_detected(client):
    now = datetime.now(timezone.utc)
    # Recent BILLING_QUEUE_JOIN with depth > 5.
    ev = make_event(event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
                    camera_id="CAM_BILLING_01", timestamp=now,
                    queue_depth=7)
    client.post("/events/ingest", json={"events": [ev]})

    body = client.get("/stores/STORE_BLR_002/anomalies").json()
    types = {a["type"] for a in body["anomalies"]}
    assert "QUEUE_SPIKE" in types


def test_dead_zone_detected_when_no_recent_visits(client):
    # Old visit (40 min ago) — should fire DEAD_ZONE.
    old = datetime.now(timezone.utc) - timedelta(minutes=40)
    ev = make_event(event_type="ZONE_ENTER", zone_id="SKINCARE",
                    camera_id="CAM_FLOOR_01", timestamp=old)
    client.post("/events/ingest", json={"events": [ev]})

    body = client.get("/stores/STORE_BLR_002/anomalies").json()
    types = {a["type"] for a in body["anomalies"]}
    # DEAD_ZONE fires for SKINCARE because no recent ZONE_ENTER.
    assert "DEAD_ZONE" in types


def test_stale_camera_detected_after_10_minutes(client):
    old = datetime.now(timezone.utc) - timedelta(minutes=15)
    ev = make_event(event_type="ENTRY", camera_id="CAM_ENTRY_01", timestamp=old)
    client.post("/events/ingest", json={"events": [ev]})

    body = client.get("/stores/STORE_BLR_002/anomalies").json()
    types = {a["type"] for a in body["anomalies"]}
    assert "STALE_CAMERA" in types


def test_no_anomalies_when_recent_traffic(client):
    now = datetime.now(timezone.utc)
    ev = make_event(event_type="ENTRY", timestamp=now)
    client.post("/events/ingest", json={"events": [ev]})

    body = client.get("/stores/STORE_BLR_002/anomalies").json()
    types = {a["type"] for a in body["anomalies"]}
    # No queue, recent camera activity → at most DEAD_ZONE (no zone events).
    assert "STALE_CAMERA" not in types
    assert "QUEUE_SPIKE" not in types


# ---------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------
def test_health_endpoint_structure(client):
    body = client.get("/health").json()
    assert "status" in body
    assert "db_status" in body
    assert "last_event_per_store" in body
    assert "stale_feeds" in body
    assert "uptime_seconds" in body
    assert isinstance(body["last_event_per_store"], dict)
    assert isinstance(body["stale_feeds"], list)


def test_health_reports_stale_feed(client):
    old = datetime.now(timezone.utc) - timedelta(minutes=20)
    ev = make_event(event_type="ENTRY", timestamp=old)
    client.post("/events/ingest", json={"events": [ev]})

    body = client.get("/health").json()
    assert "STORE_BLR_002" in body["stale_feeds"]
    assert body["status"] in ("warning", "degraded")


def test_health_returns_ok_with_recent_event(client):
    ev = make_event(event_type="ENTRY", timestamp=datetime.now(timezone.utc))
    client.post("/events/ingest", json={"events": [ev]})
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "STORE_BLR_002" in body["last_event_per_store"]
