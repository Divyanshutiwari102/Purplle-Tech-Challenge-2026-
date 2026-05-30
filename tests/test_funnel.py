# PROMPT: "Generate pytest tests for a /funnel endpoint that returns
#          {entry_count, zone_visit_count, billing_queue_count,
#          purchase_count, ...dropoff_pct} where the unit is SESSIONS.
#          Cover: re-entries collapse to one visitor, staff are
#          excluded, zero-purchase store gives 0 purchase_count,
#          drop-off percentages are computed correctly, and the funnel
#          remains monotonic by visitor (entry >= zone >= billing)."
#
# CHANGES MADE:
#   - Asserted on integer counts and rounded dropoff floats; the AI
#     version compared raw floats and was flaky under SQLite's float
#     accumulation.
#   - Added a monotonicity sanity test that mirrors property P9.
"""/funnel endpoint behaviour tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.helpers import make_event


def _ingest(client, events):
    r = client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200, r.text
    return r.json()


def test_funnel_starts_at_zero(client):
    body = client.get("/stores/STORE_BLR_002/funnel").json()
    assert body["entry_count"] == 0
    assert body["billing_queue_count"] == 0
    assert body["purchase_count"] == 0


def test_dropoff_percentages_are_correct(client):
    base = datetime.now(timezone.utc) - timedelta(minutes=15)
    events = []
    # 4 visitors enter; 2 reach a zone; 1 reaches billing.
    for i in range(4):
        events.append(make_event(event_type="ENTRY", visitor_id=f"VIS_d{i}",
                                 timestamp=base))
    for i in range(2):
        events.append(make_event(event_type="ZONE_ENTER", visitor_id=f"VIS_d{i}",
                                 zone_id="SKINCARE",
                                 timestamp=base + timedelta(seconds=30)))
    events.append(make_event(event_type="ZONE_ENTER", visitor_id="VIS_d0",
                             zone_id="BILLING",
                             camera_id="CAM_BILLING_01",
                             timestamp=base + timedelta(minutes=2)))
    _ingest(client, events)

    f = client.get("/stores/STORE_BLR_002/funnel").json()
    assert f["entry_count"] == 4
    assert f["zone_visit_count"] == 2
    assert f["billing_queue_count"] == 1
    # 4 → 2 = 50% drop, 2 → 1 = 50% drop
    assert abs(f["entry_to_zone_dropoff_pct"] - 50.0) < 0.01
    assert abs(f["zone_to_billing_dropoff_pct"] - 50.0) < 0.01


def test_reentry_does_not_inflate_funnel(client):
    """Property P8: a re-entry must not double-count a visitor."""
    base = datetime.now(timezone.utc) - timedelta(minutes=20)
    vid = "VIS_funnel_loop"
    events = [
        make_event(event_type="ENTRY", visitor_id=vid, timestamp=base),
        make_event(event_type="ZONE_ENTER", visitor_id=vid, zone_id="MAKEUP",
                   timestamp=base + timedelta(seconds=10)),
        make_event(event_type="EXIT", visitor_id=vid,
                   timestamp=base + timedelta(minutes=5)),
        make_event(event_type="REENTRY", visitor_id=vid,
                   timestamp=base + timedelta(minutes=8)),
        make_event(event_type="ZONE_ENTER", visitor_id=vid, zone_id="MAKEUP",
                   timestamp=base + timedelta(minutes=9)),
    ]
    _ingest(client, events)
    f = client.get("/stores/STORE_BLR_002/funnel").json()
    assert f["entry_count"] == 1
    assert f["zone_visit_count"] == 1


def test_staff_excluded_from_funnel(client):
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    events = [
        make_event(event_type="ENTRY", visitor_id="VIS_real", timestamp=base),
        make_event(event_type="ENTRY", visitor_id="VIS_staff_a",
                   is_staff=True, timestamp=base + timedelta(seconds=5)),
        make_event(event_type="ENTRY", visitor_id="VIS_staff_b",
                   is_staff=True, timestamp=base + timedelta(seconds=10)),
    ]
    _ingest(client, events)
    f = client.get("/stores/STORE_BLR_002/funnel").json()
    assert f["entry_count"] == 1


def test_funnel_monotonic_per_visit(client):
    """P9: zone_visit_count <= entry_count, billing <= zone_visit."""
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    events = []
    for i in range(3):
        vid = f"VIS_mono{i}"
        events.append(make_event(event_type="ENTRY", visitor_id=vid, timestamp=base))
        if i < 2:
            events.append(make_event(event_type="ZONE_ENTER", visitor_id=vid,
                                     zone_id="SKINCARE",
                                     timestamp=base + timedelta(seconds=10)))
    _ingest(client, events)
    f = client.get("/stores/STORE_BLR_002/funnel").json()
    assert f["entry_count"] >= f["zone_visit_count"] >= f["billing_queue_count"]


def test_zero_purchase_store_returns_zero_purchase_count(client):
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    _ingest(client, [
        make_event(event_type="ENTRY", visitor_id="VIS_z1", timestamp=base),
    ])
    f = client.get("/stores/STORE_BLR_002/funnel").json()
    assert f["purchase_count"] == 0
