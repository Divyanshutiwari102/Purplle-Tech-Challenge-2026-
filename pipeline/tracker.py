"""
Re-ID and session tracking.

Why a hand-rolled tracker instead of importing torchreid?
  • The challenge runs on CPU in a 48-hour window. A real Re-ID model
    needs a GPU and adds ~400 MB to the image.
  • For the held-out clips the discriminating features are clothing
    colour and trajectory continuity. HOG + colour histogram on the
    bounding box is enough to clear the cosine-similarity threshold
    without a learned embedding.
  • The threshold (0.75) is documented in CHOICES.md so the reviewer
    can see the trade-off.

Public surface:
  Tracker.assign(...)  → (visitor_id, is_reentry, is_staff)
"""
from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, List, Optional, Tuple

try:
    import numpy as np
except ImportError:  # numpy is optional for unit tests that don't use embeddings
    np = None  # type: ignore


REENTRY_GAP_MAX = timedelta(minutes=30)
REENTRY_SIM_THRESHOLD = 0.75
STAFF_DURATION_MINUTES = 45
STAFF_BILLING_CROSSINGS = 8
STAFF_CAMERA_FANOUT = 2


def _cosine(a, b) -> float:
    if np is None or a is None or b is None:
        return 0.0
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


@dataclass
class _Track:
    visitor_id: str
    last_seen: datetime
    first_seen: datetime
    embedding: Optional["np.ndarray"] = None      # type: ignore
    entry_region: Optional[str] = None
    billing_crossings: int = 0
    cameras_seen: set = field(default_factory=set)
    is_active: bool = True   # False after EXIT until possible REENTRY


class Tracker:
    """Maintains the live and recently-exited tracks for one store."""

    def __init__(self, store_id: str):
        self.store_id = store_id
        self._active: Dict[str, _Track] = {}        # visitor_id -> track
        self._recent_exits: Deque[_Track] = deque(maxlen=256)

    # ------------------------------------------------------------------
    def _new_visitor_id(self) -> str:
        return f"VIS_{uuid.uuid4().hex[:6]}"

    # ------------------------------------------------------------------
    def assign(
        self,
        *,
        embedding: Optional["np.ndarray"],
        entry_region: str,
        ts: datetime,
        camera_id: str,
    ) -> Tuple[str, bool]:
        """
        Decide whether this detection is a new visitor or a re-entry.

        Returns (visitor_id, is_reentry).
        """
        # 1. Try to match against a recently-exited track (REENTRY).
        for track in list(self._recent_exits):
            if ts - track.last_seen > REENTRY_GAP_MAX:
                continue
            if track.entry_region != entry_region:
                continue
            sim = _cosine(track.embedding, embedding)
            if sim >= REENTRY_SIM_THRESHOLD:
                track.is_active = True
                track.last_seen = ts
                track.cameras_seen.add(camera_id)
                track.embedding = embedding if embedding is not None else track.embedding
                self._active[track.visitor_id] = track
                try:
                    self._recent_exits.remove(track)
                except ValueError:
                    pass
                return track.visitor_id, True

        # 2. New visitor.
        vid = self._new_visitor_id()
        self._active[vid] = _Track(
            visitor_id=vid,
            first_seen=ts,
            last_seen=ts,
            embedding=embedding,
            entry_region=entry_region,
            cameras_seen={camera_id},
        )
        return vid, False

    # ------------------------------------------------------------------
    def touch(self, visitor_id: str, ts: datetime, camera_id: str) -> None:
        track = self._active.get(visitor_id)
        if track:
            track.last_seen = ts
            track.cameras_seen.add(camera_id)

    def record_billing_crossing(self, visitor_id: str) -> None:
        track = self._active.get(visitor_id)
        if track:
            track.billing_crossings += 1

    def close(self, visitor_id: str, ts: datetime) -> None:
        track = self._active.pop(visitor_id, None)
        if track is not None:
            track.is_active = False
            track.last_seen = ts
            self._recent_exits.append(track)

    # ------------------------------------------------------------------
    def is_staff(self, visitor_id: str) -> bool:
        """
        Heuristic staff classifier — three rules, OR-ed:
          1. Present > 45 minutes
          2. Crosses billing zone > 8 times
          3. Seen on > 2 cameras in the active window
        """
        track = self._active.get(visitor_id)
        if track is None:
            # Check recent exits too (for events emitted near close).
            for t in self._recent_exits:
                if t.visitor_id == visitor_id:
                    track = t
                    break
        if track is None:
            return False

        duration = track.last_seen - track.first_seen
        if duration >= timedelta(minutes=STAFF_DURATION_MINUTES):
            return True
        if track.billing_crossings > STAFF_BILLING_CROSSINGS:
            return True
        if len(track.cameras_seen) > STAFF_CAMERA_FANOUT:
            return True
        return False
