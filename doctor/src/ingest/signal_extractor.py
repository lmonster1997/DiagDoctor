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
from src.graph.state import Signal


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


# ── Span-level N+1 detection ─────────────────────────────────────────


def _normalise_sql_statement(sql: str) -> str:
    """Normalise a SQL statement for comparison (collapse values and whitespace)."""
    import re as _re

    # Collapse quoted strings
    sql = _re.sub(r"'[^']*'", "?", sql)
    # Collapse numbers
    sql = _re.sub(r"\b\d+(\.\d+)?\b", "#", sql)
    # Collapse whitespace
    sql = _re.sub(r"\s+", " ", sql)
    return sql.strip().lower()


def _get_parent_span_id(span: dict[str, Any]) -> str:
    """Extract parent_span_id from a raw span dict (OTLP camelCase or snake_case)."""
    pid = str(span.get("parent_span_id", span.get("parentSpanId", "")))
    return pid


def _get_span_db_statement(span: dict[str, Any]) -> str:
    """Extract db_statement from a raw span dict (top-level or attributes)."""
    stmt = str(span.get("db_statement", span.get("dbStatement", "")))
    if stmt:
        return stmt
    attrs = span.get("attributes", {})
    if isinstance(attrs, dict):
        stmt = str(attrs.get("db.statement", attrs.get("dbStatement", "")))
    return stmt


def _detect_span_n_plus_one(
    traces: list[dict[str, Any]],
) -> list[Signal]:
    """Detect N+1 query patterns directly from raw trace spans.

    Groups spans by (parent_span_id, normalised_db_statement) and flags
    groups with count >= 3 that exhibit linear time growth.

    This complements the deduplicator's log-text-based N+1 folding by
    working at the span level, which is more reliable when logs are
    collapsed or the N+1 spans are interleaved with other operations.

    Args:
        traces: Raw trace span dicts.

    Returns:
        List of Signal objects for detected N+1 patterns.
    """
    if not traces:
        return []

    # ── Step 1: Collect spans that have both parent_span_id and db_statement ──
    db_spans: list[dict[str, Any]] = []
    for span in traces:
        db_stmt = _get_span_db_statement(span)
        parent_id = _get_parent_span_id(span)
        if db_stmt and parent_id:
            db_spans.append(span)

    if not db_spans:
        return []

    # ── Step 2: Group by (parent_span_id, normalised_db_statement) ──
    from collections import defaultdict

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for span in db_spans:
        parent_id = _get_parent_span_id(span)
        db_stmt = _get_span_db_statement(span)
        norm = _normalise_sql_statement(db_stmt)
        groups[(parent_id, norm)].append(span)

    # ── Step 3: Filter groups with count >= 3 and validate linear growth ──
    signals: list[Signal] = []
    for (parent_id, norm_stmt), children in groups.items():
        count = len(children)
        if count < settings.ingest_n1_min_count:
            continue

        # Extract durations
        durations = [float(c.get("duration_ms", 0) or 0) for c in children]
        total_duration = sum(durations)
        avg_duration = total_duration / count if count > 0 else 0.0

        # Linear growth check: total ≈ avg × count
        # Skip check if avg is near zero (no meaningful timing data)
        if avg_duration > 0.1:
            expected_total = avg_duration * count
            deviation = abs(total_duration - expected_total) / max(expected_total, 0.001)
            if deviation > settings.ingest_n1_linear_tolerance:
                continue  # Not linear — likely different queries, skip

        # ── Build Signal ──
        sample_span = children[0]
        sample_stmt = _get_span_db_statement(sample_span)
        parent_name = ""
        # Try to find the parent span to get its name
        for t in traces:
            sid = str(t.get("span_id", t.get("spanId", "")))
            if sid == parent_id:
                parent_name = str(t.get("name", t.get("operation_name", "")))
                break

        service_name = str(
            sample_span.get("service_name", sample_span.get("service", "unknown-backend"))
        )

        summary = (
            f"[×{count}] {sample_stmt[:200]} "
            f"(avg {avg_duration:.1f}ms, total {total_duration:.1f}ms, "
            f"parent={parent_name or parent_id[:8]})"
        )

        signals.append(
            Signal(
                signal_id=f"sig-n1-span-{_short_id()}",
                source="trace",
                signal_type="repeated_query",
                service_tier="backend",
                severity="warning",
                summary=summary,
                evidence_ref=parent_id,
                timestamp=sample_span.get(
                    "start", sample_span.get("start_time", "")
                ),
                metadata={
                    "n_plus_one": True,
                    "detection_method": "span_level",
                    "count": count,
                    "total_duration_ms": total_duration,
                    "avg_duration_ms": avg_duration,
                    "db_statement": sample_stmt,
                    "normalised_statement": norm_stmt,
                    "parent_span_id": parent_id,
                    "parent_span_name": parent_name,
                    "service": service_name,
                },
            )
        )

    return signals


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

    # --- Span-level N+1 detection (raw spans, not tree-based) ---
    n1_signals = _detect_span_n_plus_one(traces)
    signals.extend(n1_signals)

    # Sort: severity (error > warning > info), then timestamp
    sev_order = {"error": 0, "warning": 1, "info": 2}
    signals.sort(key=lambda s: (sev_order.get(s.severity, 99), str(s.timestamp)))
    return signals
