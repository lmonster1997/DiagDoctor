"""Client-side error reporting endpoint — bridges browser crashes to Loki.

Accepts error payloads from the frontend ErrorBoundary (and other client-side
error reporters) and funnels them into the standard Python logging → Loki
pipeline so they appear in Evidence Collector results.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/log", tags=["client-log"])


class ClientErrorPayload(BaseModel):
    """Payload from the frontend error boundary."""

    error: str = Field(..., description="Error message (e.g. TypeError: Cannot read...)")
    stack: str | None = Field(None, description="JavaScript stack trace if available")
    componentStack: str | None = Field(None, description="React component stack")
    url: str | None = Field(None, description="page URL where the error occurred")
    timestamp: str | None = Field(None, description="ISO-8601 timestamp from the browser")


@router.post("/client-error", status_code=202)
async def report_client_error(payload: ClientErrorPayload, request: Request) -> dict[str, str]:
    """Accept a client-side error report and funnel it to Loki.

    Returns 202 Accepted — fire-and-forget; the caller should not block on this.
    The error is logged via the standard logging pipeline, which the
    ``_LokiHandler`` in ``app.observability`` pushes to Loki.
    """
    log_entry: dict[str, Any] = {
        "event": "client_error",
        "error": payload.error,
    }
    if payload.stack:
        log_entry["stack"] = payload.stack
    if payload.componentStack:
        log_entry["component_stack"] = payload.componentStack
    if payload.url:
        log_entry["url"] = payload.url
    if payload.timestamp:
        log_entry["browser_ts"] = payload.timestamp

    # Include client IP for correlating with access logs.
    client_ip = request.client.host if request.client else "unknown"
    log_entry["client_ip"] = client_ip

    logger.error(json.dumps(log_entry, ensure_ascii=False))
    return {"status": "accepted"}
