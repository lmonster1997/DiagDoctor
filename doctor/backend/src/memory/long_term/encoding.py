"""Shared symptom-encoding for the episodic memory (design §4).

召回/利用三分离 (recall/utilization separation, design §4):
- The embedding vector carries ONLY ``user_report`` (natural language) -- the
  text bge-m3 is actually reliable on. Structured signals (``signal_types``,
  ``tier``) moved to Qdrant payload **filter** (precise match, not semantic
  guess); ``golden_signals.summary`` (contains code identifiers like
  ``SELECT * FROM tasks``) stays in payload only -- aligning with the
  project's own ``code_search`` principle ("semantic vectors are unreliable
  for code identifiers"). Diagnosis outputs (root_cause / fix / category /
  affected_files) likewise stay in the payload.

Index side and query side call the SAME ``build_symptom_passage`` so the two
vectors live in one symptom subspace and are truly comparable (design §4.2).
``derive_tier`` is shared so the tier FILTER is derived identically on both
sides (design §4.3 -- the old query side hard-coded ``"backend"``,
breaking symmetry).
"""

from __future__ import annotations

from src.engine.state import NormalizedEvidence


def derive_tier(evidence: NormalizedEvidence) -> str:
    """Derive the symptom tier from evidence (index & query share this).

    - cross-layer (correlations present) -> ``"cross_layer"`` (a derived
      value; the raw ``Signal.service_tier`` field is only frontend/backend).
    - otherwise -> majority vote over ``golden_signals[].service_tier``.
    - no signals -> ``"backend"`` (matches the existing default).
    """
    if evidence.correlations:
        return "cross_layer"
    tiers = [s.service_tier for s in evidence.golden_signals if s.service_tier]
    if not tiers:
        return "backend"
    return max(set(tiers), key=tiers.count)


def build_symptom_passage(evidence: NormalizedEvidence) -> str:
    """Build the symptom embedding passage (index = query, design §4.2).

    C (hybrid refactor): the vector carries ONLY ``user_report`` (natural
    language) -- the text bge-m3 is actually good at. Structured signals
    (``signal_types`` / ``tier``) moved to Qdrant payload filter (precise
    match, not semantic guess); ``golden_signals.summary`` (which contains
    code identifiers like ``SELECT * FROM tasks``) stays in payload only --
    aligning with the project's own ``code_search`` principle ("semantic
    vectors are unreliable for code identifiers"). ``derive_tier`` is still
    used by the retriever to build the tier filter.

    Index and query sides still call this SAME function (§4.2 symmetry
    holds): both embed ``user_report`` only. Empty ``user_report`` falls back
    to a placeholder so we never embed an empty string.
    """
    return evidence.user_report.strip() or "未提供症状描述"
