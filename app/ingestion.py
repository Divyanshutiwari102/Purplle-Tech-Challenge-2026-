"""
Event ingestion: validate, deduplicate, persist.

Idempotency contract: re-POSTing the same event_id is a 200 OK with the
event counted under `duplicates`, not under `ingested`. This is the
behaviour the problem statement asks for and is what an at-least-once
producer (the detection pipeline) needs.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Iterable, List, Tuple

from .db import get_connection, transaction
from .models import Event, IngestError


def _event_to_row(e: Event) -> Tuple:
    return (
        str(e.event_id),
        e.store_id,
        e.camera_id,
        e.visitor_id,
        e.event_type.value,
        e.timestamp.isoformat().replace("+00:00", "Z"),
        e.zone_id,
        int(e.dwell_ms),
        1 if e.is_staff else 0,
        float(e.confidence),
        json.dumps(e.metadata.model_dump(), separators=(",", ":")),
    )


_INSERT_SQL = """
INSERT OR IGNORE INTO events (
    event_id, store_id, camera_id, visitor_id, event_type,
    timestamp, zone_id, dwell_ms, is_staff, confidence, metadata
) VALUES (?,?,?,?,?,?,?,?,?,?,?)
"""


def ingest_events(
    events: Iterable[Event],
    pre_errors: List[IngestError] | None = None,
    db_path: str | None = None,
) -> Tuple[int, int, List[IngestError]]:
    """
    Persist a batch.

    Returns (ingested_count, duplicate_count, errors).

    INSERT OR IGNORE makes duplicate event_ids a silent no-op at the DB
    level. We compute duplicate_count as (rows_attempted - rowcount).
    """
    errors: List[IngestError] = list(pre_errors or [])
    events = list(events)
    if not events:
        return 0, 0, errors

    ingested = 0
    duplicates = 0
    conn = get_connection(db_path)

    # We insert one row at a time so that a single bad row (e.g. a CHECK
    # constraint violation that slipped past Pydantic) doesn't fail the
    # whole batch. Partial success is required by the spec.
    with transaction(db_path):
        for ev in events:
            try:
                cur = conn.execute(_INSERT_SQL, _event_to_row(ev))
                if cur.rowcount == 1:
                    ingested += 1
                    _maybe_update_session(conn, ev)
                else:
                    duplicates += 1
            except sqlite3.IntegrityError as e:
                errors.append(IngestError(event_id=str(ev.event_id), reason=str(e)))
    return ingested, duplicates, errors


def _maybe_update_session(conn: sqlite3.Connection, ev: Event) -> None:
    """
    Materialise visitor_sessions on ENTRY/EXIT/REENTRY.

    Why materialise instead of computing on the fly?
      - /funnel needs unique visitor counts per session window.
        Walking events for every request would be O(N) per query.
      - The session table is small (one row per visit) and rebuildable
        from events if it ever gets out of sync.
    """
    et = ev.event_type.value
    ts = ev.timestamp.isoformat().replace("+00:00", "Z")

    if et == "ENTRY":
        # Open a new session keyed by (visitor_id, entry_time)
        session_id = f"S_{ev.visitor_id}_{int(ev.timestamp.timestamp())}"
        conn.execute(
            """INSERT OR IGNORE INTO visitor_sessions
               (session_id, visitor_id, store_id, entry_time, is_staff)
               VALUES (?,?,?,?,?)""",
            (session_id, ev.visitor_id, ev.store_id, ts, 1 if ev.is_staff else 0),
        )
    elif et == "EXIT":
        # Close the most recent open session for this visitor.
        conn.execute(
            """UPDATE visitor_sessions
               SET exit_time = ?
               WHERE session_id = (
                   SELECT session_id FROM visitor_sessions
                   WHERE visitor_id = ? AND store_id = ? AND exit_time IS NULL
                   ORDER BY entry_time DESC LIMIT 1
               )""",
            (ts, ev.visitor_id, ev.store_id),
        )
    elif et == "REENTRY":
        # Bump reentry_count on the latest (possibly closed) session.
        conn.execute(
            """UPDATE visitor_sessions
               SET reentry_count = reentry_count + 1, exit_time = NULL
               WHERE session_id = (
                   SELECT session_id FROM visitor_sessions
                   WHERE visitor_id = ? AND store_id = ?
                   ORDER BY entry_time DESC LIMIT 1
               )""",
            (ev.visitor_id, ev.store_id),
        )
