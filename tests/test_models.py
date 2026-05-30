# PROMPT: "Tests for the Pydantic Event model. Verify zone_id rules,
#          dwell_ms > 0 for ZONE_DWELL, confidence bounds, BILLING_QUEUE_JOIN
#          requires queue_depth, and timezone-aware timestamp."
#
# CHANGES MADE:
#   - Added a happy-path test per event type to ensure validators don't
#     accidentally over-reject.
"""Schema validation tests."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.models import Event


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _base(**overrides) -> dict:
    base = {
        "event_id": str(uuid.uuid4()),
        "store_id": "STORE_X",
        "camera_id": "CAM_1",
        "visitor_id": "VIS_1",
        "event_type": "ENTRY",
        "timestamp": _ts(),
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.9,
        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }
    base.update(overrides)
    return base


def test_entry_accepts_null_zone_id():
    Event.model_validate(_base(event_type="ENTRY", zone_id=None))


def test_entry_rejects_non_null_zone_id():
    with pytest.raises(ValidationError):
        Event.model_validate(_base(event_type="ENTRY", zone_id="SKINCARE"))


def test_zone_enter_requires_zone_id():
    with pytest.raises(ValidationError):
        Event.model_validate(_base(event_type="ZONE_ENTER", zone_id=None))


def test_zone_dwell_requires_positive_dwell():
    with pytest.raises(ValidationError):
        Event.model_validate(_base(event_type="ZONE_DWELL", zone_id="X", dwell_ms=0))
    Event.model_validate(_base(event_type="ZONE_DWELL", zone_id="X", dwell_ms=1))


def test_confidence_must_be_between_zero_and_one():
    with pytest.raises(ValidationError):
        Event.model_validate(_base(confidence=-0.1))
    with pytest.raises(ValidationError):
        Event.model_validate(_base(confidence=1.1))


def test_naive_timestamp_rejected():
    with pytest.raises(ValidationError):
        Event.model_validate(_base(timestamp="2026-03-03T14:22:10"))


def test_billing_queue_join_requires_queue_depth():
    with pytest.raises(ValidationError):
        Event.model_validate(_base(
            event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
            metadata={"queue_depth": None, "sku_zone": "BILLING", "session_seq": 1},
        ))
    Event.model_validate(_base(
        event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
        metadata={"queue_depth": 4, "sku_zone": "BILLING", "session_seq": 1},
    ))


def test_event_id_must_be_uuid():
    with pytest.raises(ValidationError):
        Event.model_validate(_base(event_id="not-a-uuid"))
