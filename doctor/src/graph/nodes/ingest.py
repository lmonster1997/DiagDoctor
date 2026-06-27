"""
Ingest node — evidence normalization entry point for the LangGraph.

Wraps the pure-Python ``ingest()`` pipeline from ``src.ingest`` as a
LangGraph node function that reads ``raw_evidence`` from the state and
returns ``evidence`` (NormalizedEvidence).

This is a **non-LLM** node: all processing is deterministic rule-based
Python (denoise → dedup → tree → signals → correlate → index).
"""

from __future__ import annotations

from typing import Any

from src.graph.state import DoctorState
from src.ingest.normalizer import ingest


async def ingest_node(state: DoctorState) -> dict[str, Any]:
    """
    Ingest node: normalize raw evidence before feeding to LLM agents.

    Converts raw_evidence (user_report, logs, traces, browser_errors)
    into NormalizedEvidence with:
    - golden_signals: extracted error/slow/repeated-query signals
    - timeline: merged cross-source chronological events
    - correlations: cross-layer trace_id chains
    - raw_refs: per-item index for specialist deep-dives
    - noise_ratio: fraction of input that was filtered as noise

    The normalized evidence is much more compact than raw logs/traces,
    allowing downstream LLM agents (Triage, Specialists) to consume
    high-signal context without blowing up the prompt window.

    Args:
        state: Current DoctorState with raw_evidence populated.

    Returns:
        Dict with 'evidence' key to merge into state.
    """
    raw = state.raw_evidence
    raw_dict: dict[str, Any] = {
        "user_report": raw.user_report,
        "logs": [log.model_dump() for log in raw.logs],
        "traces": [span.model_dump() for span in raw.traces],
        "browser_errors": [err.model_dump() for err in (raw.browser_errors or [])],
    }

    normalized = ingest(raw_dict)
    return {"evidence": normalized}
