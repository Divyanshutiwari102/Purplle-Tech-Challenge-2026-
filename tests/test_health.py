# PROMPT: "Generate pytest tests for a /health endpoint that an on-call
#          engineer would check. Cover: returns 200 with required keys,
#          status flips to 'warning' when any store has not produced
#          events in 10+ minutes, last_event_per_store updates after a
#          fresh ingest, and uptime_seconds is monotonic across requests."
#
# CHANGES MADE:
#   - Replaced the AI's freezegun-based time mocking with explicit
#     past-timestamps; freezegun is heavy and the rest of the suite
#     uses real `datetime.now`, keeping things consistent.
"""Tests for /health and stale-feed detection."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from tests.helpers import make_event


REQUIRED_KEYS = {"status", "db_status", "last_event_per_store",
                 "stale_feeds", "uptime_seconds"}


def test_health_returns_required_keys(client):
    body = client.get("/health").json()
    assert REQUIRED_KEYS.issubset(body.keys())
    assert body["db_status"] == "ok"
    assert isinstance(body["uptime_seconds"], int)
    assert isinstance(body["last_event_per_store"], dict)
    assert isinstance(body["stale_feeds"], list)


def test_health_ok_with_fresh_events(client):
    client.post("/events/ingest", json={"events": [
        make_event(event_type="ENTRY",
                   timestamp=datetime.now(timezone.utc) - timedelta(seconds=5))
    ]})
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "STORE_BLR_002" in body["last_event_per_store"]


def test_health_warns_on_stale_feed(client):
    old = datetime.now(timezone.utc) - timedelta(minutes=20)
    client.post("/events/ingest", json={"events": [
        make_event(event_type="ENTRY", timestamp=old)
    ]})
    body = client.get("/health").json()
    assert body["status"] in ("warning", "degraded")
    assert "STORE_BLR_002" in body["stale_feeds"]


def test_health_uptime_is_monotonic(client):
    a = client.get("/health").json()["uptime_seconds"]
    time.sleep(1.05)
    b = client.get("/health").json()["uptime_seconds"]
    assert b >= a


def test_health_last_event_updates_after_ingest(client):
    before = client.get("/health").json()["last_event_per_store"]
    assert "STORE_BLR_002" not in before

    client.post("/events/ingest", json={"events": [
        make_event(event_type="ENTRY",
                   timestamp=datetime.now(timezone.utc) - timedelta(seconds=2))
    ]})
    after = client.get("/health").json()["last_event_per_store"]
    assert "STORE_BLR_002" in after
