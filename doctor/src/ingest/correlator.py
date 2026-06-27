"""
Correlator — links evidence across frontend/backend/DB layers.

Uses trace_id as the primary correlation key to chain:
    frontend error → backend API log → DB slow query

This cross-layer correlation is essential for diagnosing bugs like FE-020
(frontend crash whose root cause is a missing field in the backend API response).
"""

from __future__ import annotations

import uuid
from typing import Any

from src.graph.state import Correlation, Signal


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


def _get_trace_id(item: dict[str, Any]) -> str:
    """Extract trace_id, checking top-level first, then labels."""
    tid = str(item.get("trace_id", item.get("_trace_id", "")))
    if tid:
        return tid
    labels = item.get("labels")
    if isinstance(labels, dict):
        tid = str(labels.get("trace_id", ""))
        if tid:
            return tid
    return ""


def _get_service_name(item: dict[str, Any]) -> str:
    """Extract service_name, checking top-level first, then labels."""
    svc = str(item.get("service_name", item.get("service", "")))
    if svc:
        return svc
    labels = item.get("labels")
    if isinstance(labels, dict):
        svc = str(labels.get("service_name", labels.get("service", "")))
        if svc:
            return svc
    return ""


def _get_tier_from_log(log: dict[str, Any]) -> str:
    """Derive tier from a log entry."""
    svc = _get_service_name(log).lower()
    return "frontend" if "frontend" in svc else "backend"


def _get_tier_from_span(span: dict[str, Any]) -> str:
    """Derive tier from a span entry."""
    svc = _get_service_name(span).lower()
    return "frontend" if "frontend" in svc else "backend"
    return uuid.uuid4().hex[:8]


def _get_signal_ids_by_trace(
    signals: list[Signal],
) -> dict[str, list[str]]:
    """Group signal IDs by their trace_id metadata."""
    groups: dict[str, list[str]] = {}
    for sig in signals:
        trace_id = str(sig.evidence_ref or "")
        if trace_id:
            groups.setdefault(trace_id, []).append(sig.signal_id)
    return groups


def _get_tier(signal_id: str, signals: list[Signal]) -> str:
    """Get the service_tier for a given signal_id."""
    for sig in signals:
        if sig.signal_id == signal_id:
            return sig.service_tier
    return "backend"


def correlate_by_trace_id(
    logs: list[dict[str, Any]],
    traces: list[dict[str, Any]],
    browser_errors: list[dict[str, Any]] | None = None,
    golden_signals: list[Signal] | None = None,
) -> list[Correlation]:
    """
    Build cross-layer correlations using trace_id.

    Groups log entries, trace spans, and browser errors that share the same
    trace_id into Correlation objects. Each Correlation represents a causal
    chain across tiers.

    Args:
        logs: Denoised, deduplicated log entries.
        traces: Trace spans.
        browser_errors: Browser-side errors.
        golden_signals: Pre-extracted golden signals (optional).

    Returns:
        List of Correlation objects.
    """
    # Collect all items with a trace_id
    trace_groups: dict[str, dict[str, list[str]]] = {}

    for log in logs:
        trace_id = _get_trace_id(log)
        if trace_id:
            # Use 'line' as fallback for 'message' (Loki format)
            msg = str(log.get("message", log.get("line", "")))[:200]
            trace_groups.setdefault(
                trace_id,
                {
                    "frontend": [],
                    "backend": [],
                    "db": [],
                },
            )
            tier = _get_tier_from_log(log)
            trace_groups[trace_id][tier].append(msg)

    for span in traces:
        trace_id = _get_trace_id(span)
        if trace_id:
            name = str(span.get("name", span.get("operation_name", "")))[:200]
            tier = _get_tier_from_span(span)
            trace_groups.setdefault(
                trace_id,
                {
                    "frontend": [],
                    "backend": [],
                    "db": [],
                },
            )
            trace_groups[trace_id][tier].append(name)
            # DB spans
            if str(span.get("db_statement", "")):
                trace_groups[trace_id].setdefault("db", []).append(
                    str(span.get("db_statement", ""))[:200]
                )

    for err in browser_errors or []:
        trace_id = str(err.get("trace_id", ""))
        if trace_id:
            msg = str(err.get("message", ""))[:200]
            trace_groups.setdefault(
                trace_id,
                {
                    "frontend": [],
                    "backend": [],
                    "db": [],
                },
            )
            trace_groups[trace_id]["frontend"].append(f"browser_error: {msg}")

    # Build Correlation objects from trace groups
    correlations: list[Correlation] = []

    for trace_id, groups in trace_groups.items():
        has_frontend = bool(groups.get("frontend"))
        has_backend = bool(groups.get("backend"))
        has_db = bool(groups.get("db"))

        # Only create correlation if there's cross-layer signal
        single_layer = (not has_frontend and not has_backend) or (
            not has_db and not (has_frontend and has_backend)
        )
        if not (has_frontend and (has_backend or has_db)):
            if has_backend and has_db:
                pass
            elif single_layer:
                continue

        # Build signal refs
        frontend_sigs: list[str] = []
        backend_sigs: list[str] = []
        db_sigs: list[str] = []
        if golden_signals:
            for sig in golden_signals:
                if sig.evidence_ref and trace_id in str(sig.evidence_ref):
                    if sig.service_tier == "frontend":
                        frontend_sigs.append(sig.signal_id)
                    else:
                        backend_sigs.append(sig.signal_id)
                    if sig.source == "trace" and sig.metadata.get("db_statement"):
                        db_sigs.append(sig.signal_id)

        description_parts: list[str] = []
        if has_frontend:
            description_parts.append(f"前端({len(groups['frontend'])}条)")
        if has_backend:
            description_parts.append(f"后端({len(groups['backend'])}条)")
        if has_db:
            description_parts.append(f"DB({len(groups['db'])}条)")

        correlations.append(
            Correlation(
                correlation_id=f"corr-{_short_id()}",
                trace_id=trace_id,
                description=" → ".join(description_parts),
                frontend_signals=frontend_sigs,
                backend_signals=backend_sigs,
                db_signals=db_sigs,
                confidence=0.8 if has_frontend and has_backend else 0.5,
            )
        )

    return correlations
