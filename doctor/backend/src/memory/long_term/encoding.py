"""Shared symptom-encoding for the episodic memory (design §4).

召回/利用三分离 (recall/utilization separation, design §4):
- The embedding vector carries ONLY ``user_report`` (natural language) -- the
  text bge-m3 is actually reliable on. Structured signals (``signal_types``,
  ``tier``) stay in the Qdrant payload (label only); ``golden_signals.summary``
  (contains code identifiers like ``SELECT * FROM tasks``) stays in payload
  only -- aligning with the project's own ``code_search`` principle ("semantic
  vectors are unreliable for code identifiers"). Diagnosis outputs
  (root_cause / fix / category / affected_files) likewise stay in the payload.
- Tier does NOT drive a query-side filter: the query runs before diagnosis, so
  the tier is a guess that can be wrong, and a hard filter would silently drop
  cross-tier same-root cases (design reversal of §4.2/§C).

Index side and query side call the SAME ``build_symptom_passage`` so the two
vectors live in one symptom subspace and are truly comparable (design §4.2).
``derive_tier`` is retained here for eval / unit tests, but is NO LONGER stored
in the Qdrant payload (tier filter reversed, §附录 B) -- the query side does
not tier-filter recall, and a guessed tier before diagnosis would silently
drop cross-tier same-root cases.
"""

from __future__ import annotations

from src.engine.state import NormalizedEvidence


def derive_tier(evidence: NormalizedEvidence) -> str:
    """Derive the symptom tier from evidence (retained for eval / unit tests).

    Previously labelled an index-side ``symptom_tier`` payload field; that field
    was removed (tier hard filter reversed, §附录 B) -- the query side does not
    tier-filter recall, so the label is no longer stored. The function is kept
    because eval / unit tests still assert on it. NOT used as a query-side
    filter -- the query runs before diagnosis, so the tier is a guess that can
    be wrong, and filtering on it would silently drop cross-tier same-root
    cases (design reversal of §4.2/§C).

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
    vectors are unreliable for code identifiers"). The tier filter was later
    reversed (§附录 B), but the symptom vector staying NL-only is unaffected.

    Index and query sides still call this SAME function (§4.2 symmetry
    holds): both embed ``user_report`` only. Empty ``user_report`` falls back
    to a placeholder so we never embed an empty string.
    """
    return evidence.user_report.strip() or "未提供症状描述"
