"""
Tier-aware marking — labels each evidence item as frontend or backend.

This enables:
- Frontend specialists to focus on frontend signals
- Backend specialists to focus on backend signals
- Cross-layer specialists to identify tier mismatch (symptom vs. root cause)
"""

from __future__ import annotations

from typing import Any


def derive_service_tier(
    service_name: str,
    span_name: str = "",
) -> str:
    """
    Derive the service tier (frontend/backend) from metadata.

    Rules (priority order):
    1. Explicit "demo-frontend" in service_name → frontend
    2. Explicit "demo-backend" in service_name → backend
    3. Span name hints (e.g. "fetch", "click") → frontend
    4. Default → backend
    """
    svc = service_name.lower()

    if "demo-frontend" in svc or "frontend" in svc:
        return "frontend"

    if "demo-backend" in svc or "backend" in svc:
        return "backend"

    # Heuristic: span name patterns
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
    browser_errors: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Mark each evidence item with its service_tier.

    Modifies items in-place by adding `_tier` field, then returns
    separate frontend/backend partitions.

    Returns:
        Tuple of (marked_logs, marked_traces).
    """
    # Mark logs
    for log in logs:
        svc = str(log.get("service_name", log.get("service", "")))
        log["_tier"] = derive_service_tier(svc)
        log["service_tier"] = log["_tier"]

    # Mark traces
    for span in traces:
        svc = str(span.get("service_name", span.get("service", "")))
        name = str(span.get("name", ""))
        span["_tier"] = derive_service_tier(svc, name)
        span["service_tier"] = span["_tier"]

    # Mark browser errors (always frontend)
    for err in browser_errors or []:
        err["_tier"] = "frontend"
        err["service_tier"] = "frontend"

    return logs, traces
