"""
Normalizer — orchestrates the full Ingest pipeline.

Entry point: ingest(raw_evidence) → NormalizedEvidence

Pipeline:
    1. Tier-aware marking (frontend/backend labeling)
    2. Denoise (strip /health, info noise; protect frontend sparse logs)
    3. Deduplicate & Fold (collapse repeated patterns)
    4. Golden signal extraction (errors, slow spans)
    5. Cross-layer correlation (trace_id chaining)
"""

from __future__ import annotations

from typing import Any

from src.config import settings
from src.engine.state import (
    NormalizedEvidence,
)
from src.evidence.correlator import correlate_by_trace_id
from src.evidence.deduplicator import dedup_and_fold
from src.evidence.denoiser import denoise_logs
from src.evidence.signal_extractor import extract_golden_signals
from src.evidence.tier_aware import mark_tiers


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
    # Step 1: Tier-aware marking
    logs, traces = mark_tiers(raw_logs, raw_traces)

    # Step 2: Denoise (protect frontend sparse logs)
    denoised_logs = denoise_logs(logs, protect_tier="frontend")

    # Step 3: Deduplicate & Fold (collapses repeated patterns)
    folded_logs = dedup_and_fold(denoised_logs)

    # Step 4: Golden signal extraction
    signals = extract_golden_signals(
        folded_logs,
        traces,
        slow_threshold_ms=settings.ingest_slow_span_threshold_ms,
    )

    # Step 5: Cross-layer correlation
    correlations = correlate_by_trace_id(folded_logs, traces, golden_signals=signals)

    return NormalizedEvidence(
        user_report=user_report,
        golden_signals=signals,
        correlations=correlations,
        trigger_time=raw_evidence.get("trigger_time"),
        trigger_trace_ids=list(raw_evidence.get("trigger_trace_ids") or []),
    )
