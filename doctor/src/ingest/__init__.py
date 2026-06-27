"""
Evidence Ingest / Normalization layer (v2).

Transforms raw evidence (logs.json, traces.json, browser_errors.json)
into a high signal-to-noise NormalizedEvidence that LLMs can directly consume.

Responsibilities:
1. Denoise — strip health-checks, info-level noise (but preserve frontend sparse logs)
2. Deduplicate & Fold — collapse N+1 repeated SQL into "same stmt ×N"
3. Extract golden signals — error stacks, error spans, slow spans, non-2xx responses
4. Cross-layer correlation — link frontend→backend→DB via trace_id
5. Tier-aware marking — label each event as frontend/backend

Architecture:
    raw_evidence → normalizer.ingest() → NormalizedEvidence
"""

from src.ingest.normalizer import ingest

__all__ = ["ingest"]
