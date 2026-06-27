"""
Normalizer — orchestrates the full Ingest pipeline.

Entry point: ingest(raw_evidence) → NormalizedEvidence

Pipeline:
    1. Tier-aware marking (frontend/backend labeling)
    2. Denoise (strip /health, info noise; protect frontend sparse logs)
    3. Deduplicate & Fold (collapse N+1 repeated SQL)
    4. Merge timeline (cross-source event ordering)
    5. Golden signal extraction (errors, slow spans, non-2xx)
    6. Cross-layer correlation (trace_id chaining)
    7. Compute noise ratio
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
                    description=str(log.get("message", ""))[:300],
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
                    description=f"Span: {span.get('name', 'unknown')} "
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


def ingest(raw_evidence: dict[str, Any]) -> NormalizedEvidence:
    """
    Run the full Ingest pipeline on raw evidence.

    This is a **non-LLM** node — pure Python processing to prepare
    high-quality evidence for downstream LLM-based agents.

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

    # Step 3: Deduplicate & Fold
    folded_logs = dedup_and_fold(denoised_logs)

    # Step 4: Merge timeline
    timeline = _merge_timeline(folded_logs, traces, browser_errs)

    # Step 5: Golden signal extraction
    signals = extract_golden_signals(folded_logs, traces, browser_errs)

    # Step 6: Cross-layer correlation
    correlations = correlate_by_trace_id(folded_logs, traces, browser_errs, golden_signals=signals)

    # Step 7: Noise ratio
    noise_ratio = compute_noise_ratio(raw_logs, denoised_logs)

    # Count spans by tier
    frontend_spans = sum(1 for t in traces if str(t.get("_tier", "")) == "frontend")
    backend_spans = sum(1 for t in traces if str(t.get("_tier", "")) == "backend")

    # Build raw_refs index for tool-based deep-dives
    raw_refs: dict[str, Any] = {
        "logs_count": len(raw_logs),
        "traces_count": len(raw_traces),
        "browser_errors_count": len(browser_errs),
        "denoised_logs_count": len(denoised_logs),
    }

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
