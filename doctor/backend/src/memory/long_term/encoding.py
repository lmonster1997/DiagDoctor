"""Shared symptom-encoding for the episodic memory (design §4).

召回/利用三分离 (recall/utilization separation):
- The embedding vector carries ONLY query-alignable symptom semantics
  (signal types, tier, user report, golden-signal summaries). Diagnosis
  outputs (root_cause / fix / category / affected_files) live in the Qdrant
  payload, NOT in the vector -- otherwise index and query passages are
  asymmetric and retrieval yields "mixed similarity": symptoms diluted by
  code symbols on the index side, root_cause unmatched because the query
  side has none.

Index side and query side call the SAME ``build_symptom_passage`` so the two
vectors live in one symptom subspace and are truly comparable (design §4.2).
``derive_tier`` is likewise shared so the tier anchor is derived identically
on both sides (design §4.3 -- the old query side hard-coded ``"backend"``,
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

    Structure::

        [症状] 信号: {signal_types} | 层级: {tier}
        {user_report}
        {golden_signals.summary 串联}

    Only query-alignable symptom semantics go here. ``root_cause`` /
    ``fix_suggestion`` / ``category`` / ``affected_files`` are diagnosis
    outputs (or query-time unavailable) and stay in the payload -- see module
    docstring. ``golden_signals.summary`` is included because structured
    signals (e.g. ``slow_span: SELECT * FROM tasks 重复 47 次``) are more
    precise than a possibly-vague ``user_report`` and are available on both
    sides.
    """
    signal_types = sorted({s.signal_type for s in evidence.golden_signals})
    tier = derive_tier(evidence)

    meta = (
        f"[症状] 信号: {', '.join(signal_types) if signal_types else '未识别'} "
        f"| 层级: {tier}"
    )

    parts: list[str] = [meta]
    if evidence.user_report:
        parts.append(evidence.user_report)

    summaries = [s.summary for s in evidence.golden_signals if s.summary]
    if summaries:
        parts.append("；".join(summaries))

    return "\n\n".join(parts)
