"""
Structured-logging middleware.

Every request gets:
  • a trace_id (uuid4)
  • a JSON log line on stdout with: trace_id, store_id, endpoint,
    latency_ms, event_count, status_code

The log is JSON so it's grep-able and pipeline-friendly. We avoid
Python's logging module's default formatter to keep the contract tight.
"""
from __future__ import annotations

import json
import re
import sys
import time
import uuid
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_STORE_ID_RE = re.compile(r"/stores/([^/]+)")


def _emit(record: dict) -> None:
    sys.stdout.write(json.dumps(record, separators=(",", ":")) + "\n")
    sys.stdout.flush()


class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        trace_id = request.headers.get("x-trace-id") or str(uuid.uuid4())
        request.state.trace_id = trace_id
        # event_count is set by the ingest handler via request.state.
        request.state.event_count = 0

        store_match = _STORE_ID_RE.search(request.url.path)
        store_id = store_match.group(1) if store_match else None

        t0 = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["x-trace-id"] = trace_id
            return response
        finally:
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            _emit({
                "trace_id": trace_id,
                "store_id": store_id,
                "endpoint": request.url.path,
                "method": request.method,
                "latency_ms": latency_ms,
                "event_count": getattr(request.state, "event_count", 0),
                "status_code": status_code,
            })
