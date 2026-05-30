"""
FastAPI entrypoint.

Endpoints (see problem statement table):
  POST /events/ingest
  GET  /stores/{id}/metrics
  GET  /stores/{id}/funnel
  GET  /stores/{id}/heatmap
  GET  /stores/{id}/anomalies
  GET  /health

Cross-cutting concerns:
  • Structured logging middleware (see logging_mw.py)
  • Global exception handler — never leaks stack traces
  • OperationalError handler — returns HTTP 503 with a structured body
"""
from __future__ import annotations

import logging
import traceback
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import anomalies as anomalies_mod
from . import health as health_mod
from . import metrics as metrics_mod
from .camera_stream import router as camera_router
from .dashboard import broadcast_update, router as dashboard_router
from .db import OperationalError, init_db
from .ingestion import ingest_events
from .logging_mw import StructuredLoggingMiddleware
from .models import Event, IngestError, IngestRequest, IngestResponse
from .simulation import router as simulation_router

# Quiet uvicorn's default access log; we have our own.
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

app = FastAPI(
    title="Store Intelligence API",
    version="1.0.0",
    description="Real-time analytics for offline retail stores.",
)
app.add_middleware(StructuredLoggingMiddleware)

# Permissive CORS so the React frontend (and the simulated camera UI)
# can talk to us from a different origin during development.
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# SSE endpoint(s)
app.include_router(dashboard_router)

# Simulated YOLO MJPEG camera stream
app.include_router(camera_router)

# Simulation manager (replay events, control speed, broadcast SSE)
app.include_router(simulation_router)


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ---------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------
@app.exception_handler(OperationalError)
async def _db_unavailable(request: Request, exc: OperationalError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": "database_unavailable",
            "trace_id": getattr(request.state, "trace_id", None),
            "message": "The database is temporarily unavailable. Try again shortly.",
        },
    )


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    # Log the full traceback to stderr; never expose it to clients.
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "trace_id": getattr(request.state, "trace_id", None),
            "message": "An unexpected error occurred.",
        },
    )


# ---------------------------------------------------------------------
# POST /events/ingest
# ---------------------------------------------------------------------
@app.post("/events/ingest", response_model=IngestResponse)
async def ingest(request: Request) -> IngestResponse:
    """
    Validate, dedup, persist a batch of events.

    The body is parsed manually rather than via a Pydantic dependency so
    that one malformed event does not cause the whole batch to 422.
    """
    body = await request.json()
    raw_events: List[Dict[str, Any]] = (
        body.get("events", []) if isinstance(body, dict) else []
    )
    if not isinstance(raw_events, list):
        raise HTTPException(status_code=400, detail="`events` must be a list")
    if len(raw_events) > 500:
        raise HTTPException(status_code=400, detail="batch size exceeds 500")
    if not raw_events:
        raise HTTPException(status_code=422, detail="`events` must not be empty")

    valid: List[Event] = []
    errors: List[IngestError] = []
    for raw in raw_events:
        try:
            valid.append(Event.model_validate(raw))
        except ValidationError as e:
            errors.append(IngestError(
                event_id=str(raw.get("event_id")) if isinstance(raw, dict) else None,
                reason=str(e.errors()[0]["msg"]) if e.errors() else str(e),
            ))

    request.state.event_count = len(raw_events)
    ingested, duplicates, errors = ingest_events(valid, pre_errors=errors)

    # Fan-out per-store metric updates to SSE subscribers.
    if ingested > 0:
        # Group ingested events by store so each store's subscribers
        # only get the slice that's relevant to them.
        per_store: Dict[str, int] = {}
        for ev in valid:
            per_store[ev.store_id] = per_store.get(ev.store_id, 0) + 1
        for sid, count in per_store.items():
            try:
                payload = {
                    "type": "metrics",
                    "store_id": sid,
                    "ingested": count,
                    "metrics": metrics_mod.store_metrics(sid),
                }
                broadcast_update(sid, payload)
            except Exception:
                # Never let a broadcast failure break ingest.
                pass

    return IngestResponse(ingested=ingested, duplicates=duplicates, errors=errors)


# ---------------------------------------------------------------------
# GET /stores/{id}/...
# ---------------------------------------------------------------------
@app.get("/stores/{store_id}/metrics")
def get_metrics(store_id: str) -> Dict[str, Any]:
    return metrics_mod.store_metrics(store_id)


@app.get("/stores/{store_id}/funnel")
def get_funnel(store_id: str) -> Dict[str, Any]:
    return metrics_mod.store_funnel(store_id)


@app.get("/stores/{store_id}/heatmap")
def get_heatmap(store_id: str) -> Dict[str, Any]:
    zones = metrics_mod.store_heatmap(store_id)
    return {"store_id": store_id, "zones": zones}


@app.get("/stores/{store_id}/anomalies")
def get_anomalies(store_id: str) -> Dict[str, Any]:
    return {
        "store_id": store_id,
        "anomalies": anomalies_mod.detect_active(store_id),
    }


@app.get("/health")
def get_health() -> Dict[str, Any]:
    return health_mod.health()
