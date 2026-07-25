"""Pairwise retrieval ablation for the episodic memory (design §9.1/§9.3).

Empirically demonstrates P0's symptom-similarity ceiling: group case pairs by
(root-cause-type same/different) × (symptom-type same/different) into four
quadrants, then measure P0 (symptom-cosine) recall@k per quadrant.

Expected P0 behavior (the ceiling story):

| quadrant (root, symptom)        | example                              | P0 recall | verdict     |
|---------------------------------|--------------------------------------|-----------|-------------|
| same-root + same-symptom        | N+1 tasks ↔ N+1 projects             | HIGH      | ✓ correct   |
| same-root + diff-symptom        | null-assignee BE-500 ↔ FE-crash      | LOW       | ✗ ceiling   |
| diff-root + same-symptom        | FK-500 ↔ scalar-500 ↔ AttributeError | HIGH      | ✗ over-call |
| diff-root + diff-symptom        | unrelated                            | LOW       | ✓ correct   |

P1-a (``root_cause_vector``) re-runs the same quadrants by root-cause cosine
and flips the two bad quadrants (same-root-diff-symptom up, diff-root-same-
symptom down) -> the before/after ablation that motivates the dual vector.

This module is pure logic (no I/O, no embed, no Qdrant) so it is unit-testable.
The companion script ``scripts/eval_recall_ablation.py`` does the I/O: loads
the 15 cases, ingests evidence, embeds symptom passages, builds the cosine
matrix, and calls back into here for the quadrant recall.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Quadrant names: (same_root_cause, same_symptom) ──────────────────
Q_SAME_ROOT_SAME_SYM = "same_root_same_symptom"  # P0 should recall (high)
Q_SAME_ROOT_DIFF_SYM = "same_root_diff_symptom"  # P0 ceiling (low) -> P1-a fixes
Q_DIFF_ROOT_SAME_SYM = "diff_root_same_symptom"  # P0 over-recall (high) -> P1-a fixes
Q_DIFF_ROOT_DIFF_SYM = "diff_root_diff_symptom"  # correct negative (low)

_QUADRANT_ORDER = (
    Q_SAME_ROOT_SAME_SYM,
    Q_SAME_ROOT_DIFF_SYM,
    Q_DIFF_ROOT_SAME_SYM,
    Q_DIFF_ROOT_DIFF_SYM,
)

# Human-readable description per quadrant (for the report).
QUADRANT_DESCRIPTIONS: dict[str, str] = {
    Q_SAME_ROOT_SAME_SYM: "同类相似症状 (same root + same symptom) — P0 应高召回 ✓",
    Q_SAME_ROOT_DIFF_SYM: "根因似症状异 (same root + diff symptom) — P0 天花板 ✗ -> P1-a 突破",
    Q_DIFF_ROOT_SAME_SYM: "症状似根因异 (diff root + same symptom) — P0 过召回 ✗ -> P1-a 区分",
    Q_DIFF_ROOT_DIFF_SYM: "异根因异症状 (diff root + diff symptom) — 正确负样本 (应低召回) ✓",
}


@dataclass(frozen=True)
class CaseLabel:
    """A case's root-cause-type (manual) + symptom-type (from ingest).

    ``symptom_type`` is a canonical string like "backend:{slow_span,repeated_query}"
    (tier + sorted signal types) so two cases with the same tier + signal set
    compare equal.
    """

    case_id: str
    root_cause_type: str
    symptom_type: str


@dataclass(frozen=True)
class QuadrantRecall:
    """Recall@k for one quadrant, averaged over queries that have candidates in it."""

    quadrant: str
    recall_at_k: float
    query_count: int  # queries that had ≥1 candidate in this quadrant
    candidate_pairs: int  # total candidate pairs in this quadrant across queries


def quadrant_of(query: CaseLabel, candidate: CaseLabel) -> str:
    """Classify a (query, candidate) pair into one of the four quadrants."""
    same_root = query.root_cause_type == candidate.root_cause_type
    same_sym = query.symptom_type == candidate.symptom_type
    if same_root and same_sym:
        return Q_SAME_ROOT_SAME_SYM
    if same_root:
        return Q_SAME_ROOT_DIFF_SYM
    if same_sym:
        return Q_DIFF_ROOT_SAME_SYM
    return Q_DIFF_ROOT_DIFF_SYM


def build_rankings(relevance: dict[str, dict[str, float]]) -> dict[str, list[str]]:
    """Rank candidate ids per query by relevance (cosine) descending.

    ``relevance``: query_case_id -> {candidate_case_id: cosine_score}. Self-pairs
    (query == candidate) are excluded from the ranking.
    """
    rankings: dict[str, list[str]] = {}
    for query_id, scores in relevance.items():
        ranked = [
            cand
            for cand, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
            if cand != query_id
        ]
        rankings[query_id] = ranked
    return rankings


def recall_at_k_per_quadrant(
    rankings: dict[str, list[str]],
    labels: dict[str, CaseLabel],
    k: int,
) -> list[QuadrantRecall]:
    """Compute recall@k per quadrant, averaged over queries.

    Args:
        rankings: query_case_id -> candidate_case_ids ranked by relevance desc
            (self already excluded).
        labels: case_id -> CaseLabel (must cover all query + candidate ids).
        k: top-k cutoff.

    For each query, each quadrant Q gets::

        recall@k = |Q-candidates in top-k| / |Q-candidates total|

    averaged over queries that have ≥1 candidate in Q. So a quadrant that no
    query populates reports recall 0.0 with query_count 0.
    """
    sums: dict[str, float] = dict.fromkeys(_QUADRANT_ORDER, 0.0)
    query_counts: dict[str, int] = dict.fromkeys(_QUADRANT_ORDER, 0)
    pair_counts: dict[str, int] = dict.fromkeys(_QUADRANT_ORDER, 0)

    for query_id, ranked in rankings.items():
        query_label = labels.get(query_id)
        if query_label is None:
            continue
        # Partition this query's candidates by quadrant (built dynamically to
        # avoid a shared-list comprehension).
        quad_candidates: dict[str, list[str]] = {}
        for cand_id in ranked:
            cand_label = labels.get(cand_id)
            if cand_label is None or cand_id == query_id:
                continue
            quad_candidates.setdefault(quadrant_of(query_label, cand_label), []).append(cand_id)
        # recall@k per quadrant for this query.
        top_k = set(ranked[:k])
        for q in _QUADRANT_ORDER:
            cands = quad_candidates.get(q)
            if not cands:
                continue
            hit = sum(1 for c in cands if c in top_k)
            sums[q] += hit / len(cands)
            query_counts[q] += 1
            pair_counts[q] += len(cands)

    return [
        QuadrantRecall(
            quadrant=q,
            recall_at_k=(sums[q] / query_counts[q]) if query_counts[q] else 0.0,
            query_count=query_counts[q],
            candidate_pairs=pair_counts[q],
        )
        for q in _QUADRANT_ORDER
    ]


def format_quadrant_report(
    results: list[QuadrantRecall], k: int, title: str = "P0 symptom-cosine recall"
) -> str:
    """Render the per-quadrant recall table as markdown."""
    lines = [f"## {title} (recall@{k}) per quadrant", ""]
    lines.append("| 象限 | 含义 | recall@k | query 数 | 候选对数 |")
    lines.append("|---|---|---|---|---|")
    for r in results:
        desc = QUADRANT_DESCRIPTIONS.get(r.quadrant, r.quadrant)
        lines.append(
            f"| {r.quadrant} | {desc} | {r.recall_at_k:.2f} | "
            f"{r.query_count} | {r.candidate_pairs} |"
        )
    return "\n".join(lines)
