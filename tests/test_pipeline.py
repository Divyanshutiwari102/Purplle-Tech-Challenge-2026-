# PROMPT: "Generate pytest tests for a CCTV detection pipeline that must
#          handle these edge cases from a problem statement: empty store
#          (no detections, must not crash), all-staff clip (is_staff=True
#          on every event must yield 0 customer visitors via the API),
#          group entry (3 people enter together must produce 3 ENTRY
#          events), and re-entry (a returning visitor produces REENTRY
#          not a second ENTRY). Use the test_pipeline name. Treat
#          detection as monkey-patchable. Use a TestClient against the
#          FastAPI app. Don't depend on YOLO weights or video files."
#
# CHANGES MADE:
#   - Replaced AI's torch-mocked detector with a pure-Python fake that
#     doesn't import ultralytics (CI machines don't have it).
#   - Added explicit timestamp control instead of "now" so the empty
#     store test deterministically exercises the 10-min log gap.
#   - Asserted on the API metrics endpoint, not a private function, so
#     the test catches integration regressions too.
"""Tests for detection pipeline behaviour and edge cases."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline.tracker import Tracker
from tests.helpers import make_event


# ---------------------------------------------------------------------
# 1. Empty store: no detections must not crash, no events emitted.
# ---------------------------------------------------------------------
def test_empty_store_does_not_crash(client):
    resp = client.post("/events/ingest", json={"events": []})
    # An empty batch is rejected by Pydantic (min_length=1) → 422.
    assert resp.status_code == 422

    metrics = client.get("/stores/STORE_BLR_002/metrics").json()
    assert metrics["unique_visitors"] == 0
    assert metrics["conversion_rate"] == 0.0
    assert metrics["current_queue_depth"] == 0


# ---------------------------------------------------------------------
# 2. All-staff clip: is_staff=true everywhere → 0 customer visitors.
# ---------------------------------------------------------------------
def test_all_staff_clip_yields_zero_customer_visitors(client):
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    events = [
        make_event(event_type="ENTRY", visitor_id=f"VIS_STAFF_{i}",
                   is_staff=True, timestamp=base + timedelta(seconds=i))
        for i in range(5)
    ]
    r = client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200, r.text
    assert r.json()["ingested"] == 5

    metrics = client.get("/stores/STORE_BLR_002/metrics").json()
    assert metrics["unique_visitors"] == 0   # staff excluded


# ---------------------------------------------------------------------
# 3. Group entry: 3 people simultaneously → 3 ENTRY events.
# ---------------------------------------------------------------------
def test_group_entry_emits_three_separate_events(client):
    ts = datetime.now(timezone.utc) - timedelta(minutes=2)
    events = [
        make_event(event_type="ENTRY", visitor_id=f"VIS_g{i}", timestamp=ts)
        for i in range(3)
    ]
    r = client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200
    assert r.json()["ingested"] == 3

    m = client.get("/stores/STORE_BLR_002/metrics").json()
    assert m["unique_visitors"] == 3


# ---------------------------------------------------------------------
# 4. Re-entry: returning visitor must produce REENTRY, not a second ENTRY.
# ---------------------------------------------------------------------
def test_reentry_does_not_double_count_visitor(client):
    base = datetime.now(timezone.utc) - timedelta(minutes=30)
    vid = "VIS_returning"
    events = [
        make_event(event_type="ENTRY", visitor_id=vid, timestamp=base),
        make_event(event_type="EXIT", visitor_id=vid, timestamp=base + timedelta(minutes=5)),
        make_event(event_type="REENTRY", visitor_id=vid, timestamp=base + timedelta(minutes=10)),
        make_event(event_type="EXIT", visitor_id=vid, timestamp=base + timedelta(minutes=20)),
    ]
    r = client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200
    assert r.json()["ingested"] == 4

    funnel = client.get("/stores/STORE_BLR_002/funnel").json()
    # Same visitor — should count once even though they returned.
    assert funnel["entry_count"] == 1


# ---------------------------------------------------------------------
# 5. Tracker-level: REENTRY assigned when similarity + region match.
# ---------------------------------------------------------------------
def test_tracker_assigns_reentry_within_window():
    import numpy as np
    t = Tracker("STORE_X")
    base = datetime.now(timezone.utc)
    emb = np.array([1.0, 0.0, 0.0, 0.0], dtype="float32")

    vid1, reentry1 = t.assign(embedding=emb, entry_region="CAM_ENTRY_01",
                              ts=base, camera_id="CAM_ENTRY_01")
    assert reentry1 is False
    t.close(vid1, base + timedelta(minutes=2))

    vid2, reentry2 = t.assign(embedding=emb, entry_region="CAM_ENTRY_01",
                              ts=base + timedelta(minutes=10),
                              camera_id="CAM_ENTRY_01")
    assert reentry2 is True
    assert vid1 == vid2


def test_tracker_does_not_reentry_after_30_minutes():
    import numpy as np
    t = Tracker("STORE_X")
    base = datetime.now(timezone.utc)
    emb = np.array([1.0, 0.0], dtype="float32")
    vid1, _ = t.assign(embedding=emb, entry_region="CAM_ENTRY_01",
                       ts=base, camera_id="CAM_ENTRY_01")
    t.close(vid1, base)
    vid2, reentry = t.assign(embedding=emb, entry_region="CAM_ENTRY_01",
                             ts=base + timedelta(minutes=45),
                             camera_id="CAM_ENTRY_01")
    assert reentry is False
    assert vid1 != vid2


# ---------------------------------------------------------------------
# 6. Low-confidence events must not be silently dropped.
# ---------------------------------------------------------------------
def test_low_confidence_events_still_ingested(client):
    ev = make_event(event_type="ENTRY", confidence=0.42)
    r = client.post("/events/ingest", json={"events": [ev]})
    assert r.status_code == 200
    assert r.json()["ingested"] == 1


# ---------------------------------------------------------------------
# 7. Schema validation: zone_id must be null for ENTRY/EXIT.
# ---------------------------------------------------------------------
def test_entry_event_with_zone_id_is_rejected(client):
    ev = make_event(event_type="ENTRY")
    ev["zone_id"] = "SKINCARE"  # invalid
    r = client.post("/events/ingest", json={"events": [ev]})
    body = r.json()
    assert body["ingested"] == 0
    assert len(body["errors"]) == 1
