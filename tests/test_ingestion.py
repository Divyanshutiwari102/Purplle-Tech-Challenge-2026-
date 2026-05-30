# PROMPT: "Generate pytest tests for an idempotent batch event ingestion
#          API. Cover: a 500-event batch is accepted as one call, a
#          batch over 500 is rejected, an empty batch is rejected (422),
#          partial-failure with malformed events still succeeds with a
#          structured `errors` list, replays of the same payload return
#          duplicates count not 5xx, and ENTRY events materialise a
#          visitor_session row in the DB."
#
# CHANGES MADE:
#   - Replaced AI's mock SQLAlchemy session with the real DB used by the
#     TestClient fixture so the test exercises the materialisation path.
#   - Added a stronger over-limit test (501 events) to lock in the
#     boundary condition the spec calls out.
"""Ingestion tier tests: validation, idempotency, batch limits, sessions."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from tests.helpers import make_event


def test_batch_of_500_is_accepted(client):
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    events = [
        make_event(event_type="ENTRY", visitor_id=f"VIS_b{i}",
                   timestamp=base + timedelta(seconds=i))
        for i in range(500)
    ]
    r = client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200
    body = r.json()
    assert body["ingested"] == 500
    assert body["duplicates"] == 0
    assert body["errors"] == []


def test_batch_over_500_is_rejected(client):
    base = datetime.now(timezone.utc)
    events = [make_event(event_type="ENTRY", visitor_id=f"VIS_x{i}",
                         timestamp=base + timedelta(seconds=i))
              for i in range(501)]
    r = client.post("/events/ingest", json={"events": events})
    assert r.status_code == 400
    assert "500" in r.json()["detail"]


def test_empty_batch_is_rejected(client):
    assert client.post("/events/ingest", json={"events": []}).status_code == 422


def test_non_list_events_field_returns_400(client):
    r = client.post("/events/ingest", json={"events": "not-a-list"})
    assert r.status_code == 400


def test_replay_is_idempotent(client):
    ev = make_event(event_type="ENTRY")
    a = client.post("/events/ingest", json={"events": [ev]}).json()
    b = client.post("/events/ingest", json={"events": [ev]}).json()
    c = client.post("/events/ingest", json={"events": [ev]}).json()
    assert a["ingested"] == 1
    assert b["ingested"] == 0 and b["duplicates"] == 1
    assert c["ingested"] == 0 and c["duplicates"] == 1


def test_partial_failure_keeps_valid_events(client):
    good = make_event(event_type="ENTRY")
    bad = {**good, "event_id": "not-a-uuid"}    # invalid uuid
    body = client.post("/events/ingest", json={"events": [good, bad]}).json()
    assert body["ingested"] == 1
    assert len(body["errors"]) == 1
    assert body["errors"][0]["reason"]


def test_session_row_materialised_on_entry(client, db_path):
    base = datetime.now(timezone.utc) - timedelta(minutes=2)
    vid = "VIS_session_test"
    client.post("/events/ingest", json={"events": [
        make_event(event_type="ENTRY", visitor_id=vid, timestamp=base),
    ]})
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT visitor_id, store_id FROM visitor_sessions WHERE visitor_id=?",
        (vid,),
    ).fetchall()
    conn.close()
    assert rows and rows[0][0] == vid


def test_exit_event_closes_session(client, db_path):
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    vid = "VIS_close_test"
    client.post("/events/ingest", json={"events": [
        make_event(event_type="ENTRY", visitor_id=vid, timestamp=base),
        make_event(event_type="EXIT", visitor_id=vid,
                   timestamp=base + timedelta(minutes=5)),
    ]})
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT exit_time FROM visitor_sessions WHERE visitor_id=?", (vid,)
    ).fetchone()
    conn.close()
    assert row and row[0] is not None
