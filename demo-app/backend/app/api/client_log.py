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


class BreadcrumbEntry(BaseModel):
    """A single user-action breadcrumb leading up to the error."""

    category: str = Field(
        ..., description="click | navigation | network | input | lifecycle | custom"
    )
    message: str = Field(..., description="Human-readable description of the action")
    timestamp: str = Field(..., description="ISO-8601 timestamp")
    data: dict[str, object] | None = Field(None, description="Optional structured metadata")


class ClientErrorPayload(BaseModel):
    """Payload from the frontend error boundary or global error hooks."""

    error: str = Field(..., description="Error message (e.g. TypeError: Cannot read...)")
    stack: str | None = Field(None, description="JavaScript stack trace if available")
    componentStack: str | None = Field(None, description="React component stack")  # noqa: N815
    url: str | None = Field(None, description="page URL where the error occurred")
    timestamp: str | None = Field(None, description="ISO-8601 timestamp from the browser")
    trace_id: str | None = Field(
        None, description="OTel trace_id for cross-tier correlation (hex, 32 chars)"
    )
    span_id: str | None = Field(
        None, description="OTel span_id for cross-tier correlation (hex, 16 chars)"
    )
    breadcrumbs: list[BreadcrumbEntry] = Field(
        default_factory=list, description="User actions leading up to the error"
    )


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
    if payload.trace_id:
        log_entry["trace_id"] = payload.trace_id
    if payload.span_id:
        log_entry["span_id"] = payload.span_id
    if payload.breadcrumbs:
        log_entry["breadcrumbs"] = [b.model_dump() for b in payload.breadcrumbs]

    # Include client IP for correlating with access logs.
    client_ip = request.client.host if request.client else "unknown"
    log_entry["client_ip"] = client_ip

    logger.error(json.dumps(log_entry, ensure_ascii=False))
    return {"status": "accepted"}
