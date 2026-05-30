"""
Server-Sent Events (SSE) for real-time dashboard updates.

Why SSE and not WebSockets?
  - One-way server → client is what the dashboard needs.
  - SSE rides on plain HTTP, no upgrade handshake, works through
    proxies, supports automatic reconnect on the browser side.
  - Lighter than a WebSocket message router for our scale.

Design:
  - One asyncio.Queue per subscriber (per store).
  - `broadcast_update(store_id, payload)` fan-outs to every queue
    for that store. Slow clients are dropped (queue.put_nowait).
  - The endpoint streams `data: <json>\n\n` lines until the client
    disconnects.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter()

# Per-store list of subscriber queues. Bounded so a slow client can't
# OOM the API.
_QUEUE_MAXSIZE = 32
_subscribers: Dict[str, List[asyncio.Queue]] = {}


def _queues_for(store_id: str) -> List[asyncio.Queue]:
    return _subscribers.setdefault(store_id, [])


def broadcast_update(store_id: str, payload: Dict[str, Any]) -> None:
    """
    Push an event to every subscriber listening on this store.

    Safe to call from sync code (e.g. the ingest handler).
    """
    msg = json.dumps(payload, separators=(",", ":"))
    dead: List[asyncio.Queue] = []
    for q in _queues_for(store_id):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            # Slow consumer: drop the queue, browser will reconnect.
            dead.append(q)
    if dead:
        live = [q for q in _queues_for(store_id) if q not in dead]
        _subscribers[store_id] = live


async def _event_stream(request: Request, store_id: str):
    q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    _queues_for(store_id).append(q)

    # Initial hello + heartbeat scheduling.
    await q.put(json.dumps({
        "type": "hello",
        "store_id": store_id,
        "ts": time.time(),
    }))

    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                msg = await asyncio.wait_for(q.get(), timeout=15.0)
                yield f"data: {msg}\n\n"
            except asyncio.TimeoutError:
                # Heartbeat keeps idle proxies from killing the connection.
                yield ": heartbeat\n\n"
    finally:
        try:
            _queues_for(store_id).remove(q)
        except ValueError:
            pass


@router.get("/stores/{store_id}/stream")
async def stream(store_id: str, request: Request) -> StreamingResponse:
    """
    SSE stream of metric updates for a store.

    The browser connects via `new EventSource(...)` and receives a
    JSON payload every time `/events/ingest` accepts events for this
    store, plus heartbeats every 15 s.
    """
    return StreamingResponse(
        _event_stream(request, store_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering
        },
    )
