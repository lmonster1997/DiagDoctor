"""
Tier-aware marking — labels each evidence item as frontend or backend.

This enables:
- Frontend specialists to focus on frontend signals
- Backend specialists to focus on backend signals
- Cross-layer specialists to identify tier mismatch (symptom vs. root cause)
"""

from __future__ import annotations

from typing import Any

from src.config import settings


def _get_service_name(item: dict[str, Any]) -> str:
    """Extract service_name from item, checking top-level first, then labels.

    Checks each source independently — empty strings do NOT shadow
    populated fallback fields (e.g. ``service_name=""`` won't hide
    ``service="demo-backend"``).
    """
    # Top-level: service_name, then service (only if non-empty)
    svc = str(item.get("service_name", "")).strip()
    if svc:
        return svc
    svc = str(item.get("service", "")).strip()
    if svc:
        return svc
    # Labels: same priority order
    labels = item.get("labels")
    if isinstance(labels, dict):
        svc = str(labels.get("service_name", "")).strip()
        if svc:
            return svc
        svc = str(labels.get("service", "")).strip()
        if svc:
            return svc
    return ""


def _get_trace_id(item: dict[str, Any]) -> str:
    """Extract trace_id from item, checking top-level first, then labels.

    Checks each source independently so empty strings don't shadow
    populated fallback fields.
    """
    tid = str(item.get("trace_id", "")).strip()
    if tid:
        return tid
    labels = item.get("labels")
    if isinstance(labels, dict):
        tid = str(labels.get("trace_id", "")).strip()
        if tid:
            return tid
    return ""


def derive_service_tier(
    service_name: str,
    span_name: str = "",
) -> str:
    """
    Derive the service tier (frontend/backend) from metadata.

    Rules (priority order):
    1. Service name matches configured frontend/backend service name
    2. Generic "frontend"/"backend" in service_name
    3. Span name hints (e.g. "fetch", "click") → frontend (OTel standard)
    4. Default → backend
    """
    svc = service_name.lower()

    # Config-driven: match against configured service names
    if settings.frontend_service_name.lower() in svc:
        return "frontend"
    if settings.backend_service_name.lower() in svc:
        return "backend"

    # Generic fallback: common naming conventions
    if "frontend" in svc:
        return "frontend"
    if "backend" in svc:
        return "backend"

    # Heuristic: OTel standard span name patterns
    sn = span_name.lower()
    frontend_patterns = [
        "fetch",
        "xhr",
        "click",
        "navigate",
        "documentload",
        "user_interaction",
        "route_change",
        "component",
    ]
    for pattern in frontend_patterns:
        if pattern in sn:
            return "frontend"

    return "backend"


def mark_tiers(
    logs: list[dict[str, Any]],
    traces: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Mark each evidence item with its service_tier.

    Modifies items in-place by adding `_tier` field.

    Returns:
        Tuple of (marked_logs, marked_traces).
    """
    # Mark logs (check top-level + labels.service_name)
    for log in logs:
        svc = _get_service_name(log)
        log["_tier"] = derive_service_tier(svc)
        log["service_tier"] = log["_tier"]
        # Normalise: ensure top-level service_name is populated for downstream
        if not log.get("service_name"):
            log["service_name"] = svc
        # Set _ref for correlation (prefer trace_id from labels or body)
        if not log.get("_ref"):
            tid = _get_trace_id(log)
            if tid:
                log["_ref"] = tid

    # Mark traces (check top-level + labels, use operation_name fallback for name)
    for span in traces:
        svc = _get_service_name(span)
        name = str(span.get("name", span.get("operation_name", "")))
        span["_tier"] = derive_service_tier(svc, name)
        span["service_tier"] = span["_tier"]
        # Normalise: ensure top-level fields are populated for downstream consumers
        if not span.get("service_name"):
            span["service_name"] = svc
        if not span.get("name") and span.get("operation_name"):
            span["name"] = span["operation_name"]
        # Set _ref for correlation
        if not span.get("_ref"):
            tid = _get_trace_id(span)
            if tid:
                span["_ref"] = tid

    return logs, traces
