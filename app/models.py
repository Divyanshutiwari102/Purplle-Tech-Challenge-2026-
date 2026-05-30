"""
Pydantic v2 models for the event schema.

The schema is dictated by the problem statement; this file is the single
source of truth for what a valid event looks like. The detection pipeline
emits these, and POST /events/ingest validates against them.

Validation rules (enforced here, NOT in the route handler):
  • event_id must be a UUID (v4 preferred, v1/v3/v5 also pass uuid.UUID)
  • zone_id MUST be null for ENTRY/EXIT, MUST be set otherwise
  • dwell_ms MUST be > 0 for ZONE_DWELL
  • confidence in [0.0, 1.0]
  • timestamp must be timezone-aware UTC (ISO-8601 with Z or +00:00)
  • metadata.queue_depth MUST be an int when event_type is BILLING_QUEUE_JOIN
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


# ---------------------------------------------------------------------
# Enum: event types from the problem catalogue.
# String-valued so JSON round-trips are stable.
# ---------------------------------------------------------------------
class EventType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


_ENTRY_EXIT = {EventType.ENTRY, EventType.EXIT, EventType.REENTRY}


class EventMetadata(BaseModel):
    """metadata sub-object of an event."""
    model_config = ConfigDict(extra="allow")  # forward-compat: don't reject unknown keys

    queue_depth: Optional[int] = Field(default=None, ge=0)
    sku_zone: Optional[str] = None
    session_seq: int = Field(ge=0)


class Event(BaseModel):
    """Single detection event. Matches the contract in the problem statement."""
    model_config = ConfigDict(
        # extra='forbid' would be strict, but the problem says new fields may
        # arrive (e.g. future event types). We allow but don't validate them.
        extra="allow",
        str_strip_whitespace=True,
    )

    event_id: UUID
    store_id: str = Field(min_length=1)
    camera_id: str = Field(min_length=1)
    visitor_id: str = Field(min_length=1)
    event_type: EventType
    timestamp: datetime
    zone_id: Optional[str] = None
    dwell_ms: int = Field(ge=0)
    is_staff: bool
    confidence: float = Field(ge=0.0, le=1.0)
    metadata: EventMetadata

    # ---- field validators ------------------------------------------------
    @field_validator("timestamp")
    @classmethod
    def _ts_must_be_utc(cls, v: datetime) -> datetime:
        # Reject naive timestamps. Convert tz-aware to UTC.
        if v.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware ISO-8601 (UTC)")
        return v.astimezone(timezone.utc)

    # ---- cross-field validator ------------------------------------------
    @model_validator(mode="after")
    def _check_event_type_constraints(self) -> "Event":
        # Rule: zone_id is null for ENTRY/EXIT/REENTRY (threshold events),
        # required for all other types.
        if self.event_type in _ENTRY_EXIT:
            if self.zone_id is not None:
                raise ValueError(
                    f"zone_id must be null for {self.event_type.value} events"
                )
        else:
            if self.zone_id is None or self.zone_id == "":
                raise ValueError(
                    f"zone_id is required for {self.event_type.value} events"
                )

        # Rule: ZONE_DWELL must have dwell_ms > 0
        if self.event_type == EventType.ZONE_DWELL and self.dwell_ms <= 0:
            raise ValueError("dwell_ms must be > 0 for ZONE_DWELL events")

        # Rule: BILLING_QUEUE_JOIN must populate queue_depth
        if self.event_type == EventType.BILLING_QUEUE_JOIN:
            if self.metadata.queue_depth is None:
                raise ValueError(
                    "metadata.queue_depth is required for BILLING_QUEUE_JOIN"
                )
        return self


# ---------------------------------------------------------------------
# Ingest request / response shapes
# ---------------------------------------------------------------------
class IngestRequest(BaseModel):
    events: List[Event] = Field(max_length=500, min_length=1)


class IngestError(BaseModel):
    event_id: Optional[str] = None  # may be missing if event_id itself was malformed
    reason: str


class IngestResponse(BaseModel):
    ingested: int
    duplicates: int
    errors: List[IngestError]
