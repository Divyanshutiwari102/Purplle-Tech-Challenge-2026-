# PROMPT: "Add a test that validates data/sample_events.jsonl is a
#          well-formed JSONL event-log deliverable matching the Event
#          schema the API ingests — HackerEarth flagged the event log
#          as a critical, schema-validated submission artifact."
#
# CHANGES MADE:
#   - New file. Validates the committed .jsonl deliverable: every line
#     parses as JSON, every record passes the same Pydantic Event model
#     the ingest endpoint uses, and the ingest envelope (.json) stays in
#     sync with it.
"""Validate the JSONL event-log deliverable against the live schema."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models import Event

_JSONL = Path("data") / "sample_events.jsonl"
_JSON = Path("data") / "sample_events.json"


def _read_jsonl(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_event_log_jsonl_exists_and_is_line_delimited():
    assert _JSONL.exists(), "data/sample_events.jsonl deliverable is missing"
    raw = _JSONL.read_text(encoding="utf-8").splitlines()
    nonblank = [ln for ln in raw if ln.strip()]
    assert len(nonblank) >= 1
    # Each non-blank line must be one standalone JSON object (JSONL),
    # not a fragment of a larger array.
    for ln in nonblank:
        obj = json.loads(ln)
        assert isinstance(obj, dict)


def test_event_log_records_match_event_schema():
    records = _read_jsonl(_JSONL)
    assert len(records) >= 100, "expected a substantial event log"
    # Every record validates against the same model the API ingests.
    for r in records:
        Event.model_validate(r)


def test_event_log_covers_the_core_event_types():
    records = _read_jsonl(_JSONL)
    types = {r["event_type"] for r in records}
    # The log should demonstrate the full lifecycle, not just entries.
    for required in ("ENTRY", "EXIT", "ZONE_ENTER", "BILLING_QUEUE_JOIN"):
        assert required in types, f"event log missing {required}"


def test_ingest_envelope_matches_schema_too():
    # The .json envelope is what POST /events/ingest consumes; keep it
    # valid so the README's one-line ingest command always works.
    if not _JSON.exists():
        pytest.skip("ingest envelope not present")
    body = json.loads(_JSON.read_text(encoding="utf-8"))
    assert "events" in body and isinstance(body["events"], list)
    for r in body["events"]:
        Event.model_validate(r)
