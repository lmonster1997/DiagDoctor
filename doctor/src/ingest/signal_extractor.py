"""
Signal Extractor — identifies "golden signals" from normalized evidence.

Golden signals are the critical clues that an LLM needs to diagnose the bug:

Error-signal bugs (crashes, 5xx, slow queries):
    - error_log      — ERROR/WARNING logs
    - error_span     — trace spans with status=error
    - slow_span      — spans above a duration threshold
    - repeated_query — N+1 patterns (collapsed by deduplicator)

"Smokeless" bugs (logic, data, config — no error signals):
    No signals are extracted from observability data for these bugs —
    logs/traces/browser_errors all appear normal (200 OK, no errors).
    Diagnosis must rely on user_report semantic analysis + active
    investigation (code search, API probing).
"""

from __future__ import annotations

import uuid
from typing import Any

from src.graph.state import Signal


def _short_id() -> str:
    """Generate a short unique ID for a signal."""
    return uuid.uuid4().hex[:8]


def extract_golden_signals(
    logs: list[dict[str, Any]],
    traces: list[dict[str, Any]],
    browser_errors: list[dict[str, Any]] | None = None,
    slow_threshold_ms: float = 200.0,
) -> list[Signal]:
    """
    Extract golden signals from observability evidence.

    Note: "smokeless" bugs (logic/data/config) produce no signals here —
    their logs/traces/browser_errors are all normal.  The Triage agent
    must detect them from the user_report text and then actively
    investigate (code search, API probing).

    Args:
        logs: Denoised log entries.
        traces: Trace spans.
        browser_errors: Browser-side errors (Playwright/OTel-JS).
        slow_threshold_ms: Spans slower than this are flagged.

    Returns:
        List of Signal objects, ordered by severity.
    """
    signals: list[Signal] = []

    # --- From logs ---
    for log in logs:
        level = str(log.get("level", "INFO")).upper()
        if level in ("ERROR", "WARNING"):
            service_name = str(log.get("service_name", log.get("service", "")))
            tier: str = "frontend" if "frontend" in service_name.lower() else "backend"
            signals.append(
                Signal(
                    signal_id=f"sig-log-{_short_id()}",
                    source="log",
                    signal_type="error_log",
                    service_tier=tier,  # type: ignore[arg-type]
                    severity="error" if level == "ERROR" else "warning",
                    summary=str(log.get("message", ""))[:300],
                    evidence_ref=str(log.get("_ref", "")),
                    timestamp=log.get("timestamp", ""),
                    metadata={"level": level, "service": service_name},
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
                    summary=f"Error span: {span.get('name', 'unknown')} ({duration:.1f}ms)",
                    evidence_ref=str(span.get("span_id", "")),
                    timestamp=span.get("start", ""),
                    metadata={
                        "span_name": span.get("name", ""),
                        "duration_ms": duration,
                        "service": service_name,
                    },
                )
            )
        elif duration >= slow_threshold_ms:
            db_stmt = str(span.get("db_statement", ""))
            summary = f"Slow span: {span.get('name', 'unknown')} ({duration:.1f}ms)"
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
                    timestamp=span.get("start", ""),
                    metadata={
                        "span_name": span.get("name", ""),
                        "duration_ms": duration,
                        "service": service_name,
                        "db_statement": db_stmt,
                    },
                )
            )

    # --- From browser errors ---
    for err in browser_errors or []:
        msg = str(err.get("message", ""))
        signals.append(
            Signal(
                signal_id=f"sig-browser-{_short_id()}",
                source="browser_error",
                signal_type="error_log",
                service_tier="frontend",
                severity="error",
                summary=msg[:300],
                evidence_ref=str(err.get("trace_id", err.get("span_id", ""))),
                timestamp=err.get("timestamp", ""),
                metadata={
                    "stack": err.get("stack", ""),
                    "component_stack": err.get("component_stack", ""),
                },
            )
        )

    # Sort: errors first, then warnings
    signals.sort(key=lambda s: (0 if s.severity == "error" else 1, str(s.timestamp)))
    return signals
