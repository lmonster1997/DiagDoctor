"""
Normalizer — orchestrates the full Ingest pipeline.

Entry point: ingest(raw_evidence) → NormalizedEvidence

Pipeline:
    1. Tier-aware marking (frontend/backend labeling)
    2. Denoise (strip /health, info noise; protect frontend sparse logs)
    3. Deduplicate & Fold (collapse N+1 repeated SQL)
    4. Build cross-tier span tree (frontend fetch → backend server parent-child)
    5. Merge timeline (cross-source event ordering)
    6. Golden signal extraction (errors, slow spans, non-2xx, N+1 patterns)
    7. Cross-layer correlation (trace_id chaining)
    8. Compute noise ratio
    9. Build raw_refs index (per-item references for specialist deep-dives)
"""

from __future__ import annotations

from typing import Any

from src.graph.state import (
    NormalizedEvidence,
    TimelineEvent,
)
from src.ingest.correlator import correlate_by_trace_id
from src.ingest.deduplicator import dedup_and_fold
from src.ingest.denoiser import compute_noise_ratio, denoise_logs
from src.ingest.signal_extractor import extract_golden_signals
from src.ingest.tier_aware import mark_tiers
from src.tools.trace_query import build_cross_tier_tree, detect_n_plus_one, get_tree_summary


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


def _merge_timeline(
    logs: list[dict[str, Any]],
    traces: list[dict[str, Any]],
    browser_errors: list[dict[str, Any]] | None = None,
) -> list[TimelineEvent]:
    """Merge all evidence sources into a single chronological timeline."""
    events: list[tuple[str, TimelineEvent]] = []

    for log in logs:
        ts = str(log.get("timestamp", ""))
        events.append(
            (
                ts,
                TimelineEvent(
                    timestamp=ts,
                    source="log",
                    service_tier=str(log.get("_tier", "backend")),  # type: ignore[arg-type]
                    service_name=str(log.get("service_name", log.get("service", ""))),
                    description=str(log.get("message", log.get("line", "")))[:300],
                    evidence_ref=str(log.get("_ref", "")),
                    trace_id=str(log.get("trace_id", "") or None),
                ),
            )
        )

    for span in traces:
        ts = str(span.get("start", ""))
        events.append(
            (
                ts,
                TimelineEvent(
                    timestamp=ts,
                    source="trace",
                    service_tier=str(span.get("_tier", "backend")),  # type: ignore[arg-type]
                    service_name=str(span.get("service_name", span.get("service", ""))),
                    description=f"Span: {span.get('name', span.get('operation_name', 'unknown'))} "
                    f"({float(span.get('duration_ms', 0) or 0):.1f}ms)",
                    evidence_ref=str(span.get("span_id", "")),
                    trace_id=str(span.get("trace_id", "") or None),
                ),
            )
        )

    for err in browser_errors or []:
        ts = str(err.get("timestamp", ""))
        events.append(
            (
                ts,
                TimelineEvent(
                    timestamp=ts,
                    source="browser_error",
                    service_tier="frontend",
                    service_name="demo-frontend",
                    description=f"Browser Error: {str(err.get('message', ''))[:300]}",
                    evidence_ref=str(err.get("trace_id", err.get("span_id", ""))),
                    trace_id=str(err.get("trace_id", "") or None),
                ),
            )
        )

    # Sort by timestamp (string sort works for ISO format)
    events.sort(key=lambda x: x[0])
    return [e[1] for e in events]


def _index_raw(
    raw_logs: list[dict[str, Any]],
    raw_traces: list[dict[str, Any]],
    browser_errs: list[dict[str, Any]],
    folded_logs: list[dict[str, Any]],
    tree_summary: dict[str, Any],
) -> dict[str, Any]:
    """
    Build an index of raw evidence for specialist deep-dives.

    Rather than stuffing all raw evidence into the LLM prompt, we store
    a lightweight index with per-item references. Specialists can then
    use tools (log_search, trace_query) to fetch specific items by ID.

    The index includes:
    - Count summaries for quick sizing
    - Per-item refs keyed by evidence_ref ID (for O(1) lookup)
    - Span tree summary for structural context
    - Bucketed error/slow log refs for quick access
    """
    raw_refs: dict[str, Any] = {
        "counts": {
            "raw_logs": len(raw_logs),
            "denoised_logs": len(folded_logs),
            "raw_traces": len(raw_traces),
            "browser_errors": len(browser_errs),
        },
        "tree_summary": tree_summary,
    }

    # Index logs by evidence_ref for fast lookup
    log_index: dict[str, dict[str, Any]] = {}
    error_log_refs: list[str] = []
    warn_log_refs: list[str] = []
    for log in folded_logs:
        ref = str(log.get("_ref", log.get("trace_id", "")))
        if ref:
            log_index[ref] = {
                "level": _get_level(log),
                "message": str(log.get("message", log.get("line", "")))[:200],
                "service_tier": log.get("_tier", "backend"),
                "timestamp": log.get("timestamp", ""),
                "trace_id": log.get("trace_id", ""),
            }
            level = str(log.get("level", "")).upper()
            if level == "ERROR":
                error_log_refs.append(ref)
            elif level == "WARNING":
                warn_log_refs.append(ref)

    # Index traces by span_id for fast lookup
    span_index: dict[str, dict[str, Any]] = {}
    for span in raw_traces:
        sid = str(span.get("span_id", span.get("spanId", "")))
        if sid:
            span_index[sid] = {
                "name": span.get("name", span.get("operation_name", "")),
                "service_tier": span.get("_tier", "backend"),
                "duration_ms": span.get("duration_ms", span.get("durationMs", 0)),
                "status": span.get("status", "unset"),
                "db_statement": str(span.get("db_statement", span.get("dbStatement", "")))[:300],
            }

    # Index browser errors
    browser_refs: list[dict[str, Any]] = []
    for err in browser_errs:
        browser_refs.append(
            {
                "message": str(err.get("message", ""))[:200],
                "stack": str(err.get("stack", ""))[:500],
                "component_stack": str(err.get("component_stack", ""))[:300],
                "trace_id": err.get("trace_id", ""),
            }
        )

    raw_refs["log_index"] = log_index
    raw_refs["span_index"] = span_index
    raw_refs["browser_refs"] = browser_refs
    raw_refs["error_log_refs"] = error_log_refs
    raw_refs["warn_log_refs"] = warn_log_refs

    return raw_refs


def ingest(raw_evidence: dict[str, Any]) -> NormalizedEvidence:
    """
    Run the full Ingest pipeline on raw evidence.

    This is a **non-LLM** node — pure Python processing to prepare
    high-quality evidence for downstream LLM-based agents.

    Pipeline steps:
        1. Tier-aware marking (frontend/backend labeling)
        2. Denoise (strip /health, info noise; protect frontend sparse logs)
        3. Deduplicate & Fold (collapse N+1 repeated SQL)
        4. Build cross-tier span tree (frontend fetch → backend server)
        5. Merge timeline (cross-source event ordering)
        6. Golden signal extraction (errors, slow spans, N+1 patterns)
        7. Cross-layer correlation (trace_id chaining)
        8. Compute noise ratio
        9. Build raw_refs index (per-item refs for specialist deep-dives)

    Args:
        raw_evidence: Dict with keys:
            - user_report (str)
            - logs (list[dict])
            - traces (list[dict])
            - browser_errors (list[dict], optional)

    Returns:
        NormalizedEvidence ready for Triage/Specialist consumption.
    """
    # Extract raw data
    user_report = str(raw_evidence.get("user_report", ""))
    raw_logs: list[dict[str, Any]] = raw_evidence.get("logs", [])
    raw_traces: list[dict[str, Any]] = raw_evidence.get("traces", [])
    browser_errs: list[dict[str, Any]] = raw_evidence.get("browser_errors", []) or []

    # Step 1: Tier-aware marking
    logs, traces = mark_tiers(raw_logs, raw_traces, browser_errs)

    # Step 2: Denoise (protect frontend sparse logs)
    denoised_logs = denoise_logs(logs, protect_tier="frontend")

    # Step 3: Deduplicate & Fold (collapses N+1 patterns)
    folded_logs = dedup_and_fold(denoised_logs)

    # Step 4: Build cross-tier span tree
    span_tree = build_cross_tier_tree(traces)
    tree_summary = get_tree_summary(span_tree)

    # Enrich signals with N+1 patterns detected from the tree
    n_plus_ones = detect_n_plus_one(span_tree)

    # Step 5: Merge timeline
    timeline = _merge_timeline(folded_logs, traces, browser_errs)

    # Step 6: Golden signal extraction
    signals = extract_golden_signals(folded_logs, traces, browser_errs)

    # Append N+1 signals to the golden_signals
    from src.graph.state import Signal

    for np1 in n_plus_ones:
        signals.append(
            Signal(
                signal_id=np1["pattern_id"],
                source="trace",
                signal_type="repeated_query",
                service_tier="backend",
                severity="warning",
                summary=(
                    f"[×{np1['count']}] {np1['db_statement'][:200]} "
                    f"(total {np1['total_duration_ms']:.1f}ms, "
                    f"parent={np1['parent_span_name']})"
                ),
                evidence_ref=np1["parent_span_id"],
                metadata={
                    "n_plus_one": True,
                    "count": np1["count"],
                    "total_duration_ms": np1["total_duration_ms"],
                    "db_statement": np1["db_statement"],
                },
            )
        )

    # Step 7: Cross-layer correlation
    correlations = correlate_by_trace_id(folded_logs, traces, browser_errs, golden_signals=signals)

    # Step 8: Noise ratio
    noise_ratio = compute_noise_ratio(raw_logs, denoised_logs)

    # Step 9: Count spans by tier
    frontend_spans = sum(1 for t in traces if str(t.get("_tier", "")) == "frontend")
    backend_spans = sum(1 for t in traces if str(t.get("_tier", "")) == "backend")

    # Step 10: Build raw_refs index for tool-based deep-dives
    raw_refs = _index_raw(raw_logs, raw_traces, browser_errs, folded_logs, tree_summary)

    return NormalizedEvidence(
        user_report=user_report,
        golden_signals=signals,
        timeline=timeline,
        correlations=correlations,
        raw_refs=raw_refs,
        noise_ratio=noise_ratio,
        frontend_span_count=frontend_spans,
        backend_span_count=backend_spans,
    )
