"""
Signal Extractor — identifies "golden signals" from normalized evidence.

Golden signals are the critical clues that an LLM needs to diagnose the bug:

Error-signal bugs (crashes, 5xx, slow queries):
    - error_log      — ERROR/WARNING logs
    - error_span     — trace spans with status=error
    - slow_span      — spans above a duration threshold
    - repeated_query — N+1 patterns (span-level detection)

"Smokeless" bugs (logic, data, config — no error signals):
    No signals are extracted from observability data for these bugs —
    logs/traces/browser_errors all appear normal (200 OK, no errors).
    Diagnosis relies on the LLM agent's user_report semantic analysis
    and active investigation (code search, API probing).

Design principle: Ingest does deterministic filtering + classification
(denoise, dedup, signal typing, N+1 counting, cross-tier correlation).
It does NOT score or prioritise signals — that's the LLM's job.
"""

from __future__ import annotations

import uuid
from typing import Any

from src.config import settings
from src.engine.state import Signal


def _short_id() -> str:
    """Generate a short unique ID for a signal."""
    return uuid.uuid4().hex[:8]


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


def _get_level(item: dict[str, Any]) -> str:
    """Extract log level, checking top-level first, then labels.detected_level."""
    lvl = str(item.get("level", ""))
    if lvl:
        return lvl
    labels = item.get("labels")
    if isinstance(labels, dict):
        lvl = str(labels.get("detected_level", labels.get("level", "")))
        if lvl:
            return lvl
    return "INFO"


def _get_span_name(span: dict[str, Any]) -> str:
    """Extract span name, checking 'name' first, then 'operation_name'."""
    name = str(span.get("name", span.get("operation_name", "")))
    return name or "unknown"


def extract_golden_signals(
    logs: list[dict[str, Any]],
    traces: list[dict[str, Any]],
    slow_threshold_ms: float = 200.0,
) -> list[Signal]:
    """
    Extract golden signals from observability evidence.

    Note: "smokeless" bugs (logic/data/config) produce no signals here —
    their logs/traces are all normal. The diagnosis agent must detect
    them from the user_report text and actively investigate
    (code search, API probing).

    Args:
        logs: Denoised log entries.
        traces: Trace spans.
        slow_threshold_ms: Spans slower than this are flagged.

    Returns:
        List of Signal objects, ordered by severity.
    """
    signals: list[Signal] = []

    # --- From logs ---
    for log in logs:
        level = _get_level(log).upper()
        if level in ("ERROR", "WARNING"):
            service_name = _get_service_name(log)
            tier: str = "frontend" if "frontend" in service_name.lower() else "backend"
            # Use 'line' field as fallback for 'message' (Loki format)
            log_content = str(log.get("message", log.get("line", "")))
            sev = "error" if level == "ERROR" else "warning"
            signals.append(
                Signal(
                    signal_id=f"sig-log-{_short_id()}",
                    source="log",
                    signal_type="error_log",
                    service_tier=tier,  # type: ignore[arg-type]
                    severity=sev,
                    summary=log_content[:300],
                    evidence_ref=str(log.get("_ref", "")),
                    timestamp=log.get("timestamp", ""),
                    metadata={
                        "level": level,
                        "service": service_name,
                    },
                )
            )

    # --- From traces ---
    for span in traces:
        status = str(span.get("status", "unset")).lower()
        duration = float(span.get("duration_ms", 0) or 0)
        service_name = str(span.get("service_name", span.get("service", "")))
        span_tier: str = "frontend" if "frontend" in service_name.lower() else "backend"

        if status == "error":
            signals.append(
                Signal(
                    signal_id=f"sig-trace-{_short_id()}",
                    source="trace",
                    signal_type="error_span",
                    service_tier=span_tier,  # type: ignore[arg-type]
                    severity="error",
                    summary=f"Error span: {_get_span_name(span)} ({duration:.1f}ms)",
                    evidence_ref=str(span.get("span_id", "")),
                    timestamp=span.get("start", span.get("start_time", "")),
                    metadata={
                        "span_name": _get_span_name(span),
                        "duration_ms": duration,
                        "service": service_name,
                    },
                )
            )
        elif duration >= slow_threshold_ms:
            db_stmt = str(span.get("db_statement", ""))
            span_name = _get_span_name(span)
            summary = f"Slow span: {span_name} ({duration:.1f}ms)"
            if db_stmt:
                summary += f" | SQL: {db_stmt[:200]}"
            signals.append(
                Signal(
                    signal_id=f"sig-slow-{_short_id()}",
                    source="trace",
                    signal_type="slow_span",
                    service_tier=span_tier,  # type: ignore[arg-type]
                    severity="warning",
                    summary=summary,
                    evidence_ref=str(span.get("span_id", "")),
                    timestamp=span.get("start", span.get("start_time", "")),
                    metadata={
                        "span_name": span_name,
                        "duration_ms": duration,
                        "service": service_name,
                        "db_statement": db_stmt,
                    },
                )
            )

    # Sort: severity (error > warning > info), then timestamp
    sev_order = {"error": 0, "warning": 1, "info": 2}
    signals.sort(key=lambda s: (sev_order.get(s.severity, 99), str(s.timestamp)))
    return signals
