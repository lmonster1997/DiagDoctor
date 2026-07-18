"""Unit tests for recall_ablation pure logic (the four-quadrant recall).

Synthetic labels + rankings verify: quadrant classification, ranking build, and
recall@k per quadrant -- including the ceiling signature (same-root-diff-symptom
recalls LOW, diff-root-same-symptom recalls HIGH) that motivates P1-a.
"""

from __future__ import annotations

from src.memory.long_term.recall_ablation import (
    Q_DIFF_ROOT_DIFF_SYM,
    Q_DIFF_ROOT_SAME_SYM,
    Q_SAME_ROOT_DIFF_SYM,
    Q_SAME_ROOT_SAME_SYM,
    CaseLabel,
    build_rankings,
    format_quadrant_report,
    quadrant_of,
    recall_at_k_per_quadrant,
)


def _label(case_id: str, root: str, sym: str) -> CaseLabel:
    return CaseLabel(case_id=case_id, root_cause_type=root, symptom_type=sym)


# ── quadrant_of ─────────────────────────────────────────────────────


def test_quadrant_of_all_four() -> None:
    a = _label("A", "n-plus-1", "backend:{slow_span}")
    same_root_same_sym = _label("B", "n-plus-1", "backend:{slow_span}")
    same_root_diff_sym = _label("C", "n-plus-1", "frontend:{browser_error}")
    diff_root_same_sym = _label("D", "fk", "backend:{slow_span}")
    diff_root_diff_sym = _label("E", "fk", "frontend:{browser_error}")

    assert quadrant_of(a, same_root_same_sym) == Q_SAME_ROOT_SAME_SYM
    assert quadrant_of(a, same_root_diff_sym) == Q_SAME_ROOT_DIFF_SYM
    assert quadrant_of(a, diff_root_same_sym) == Q_DIFF_ROOT_SAME_SYM
    assert quadrant_of(a, diff_root_diff_sym) == Q_DIFF_ROOT_DIFF_SYM


# ── build_rankings ──────────────────────────────────────────────────


def test_build_rankings_sorts_desc_and_excludes_self() -> None:
    relevance = {
        "A": {"A": 1.0, "B": 0.9, "C": 0.5, "D": 0.8},
    }
    rankings = build_rankings(relevance)
    assert rankings["A"] == ["B", "D", "C"]  # 0.9, 0.8, 0.5; self A excluded


# ── recall_at_k_per_quadrant: the ceiling signature ─────────────────


def test_recall_at_k_shows_ceiling_signature() -> None:
    """One query A with three candidates demonstrates the P0 ceiling.

    A = (root1, sym1). Candidates:
      B = (root1, sym1) -> same-root-same-symptom  (should recall)
      C = (root1, sym2) -> same-root-diff-symptom  (P0 ceiling: missed)
      D = (root2, sym1) -> diff-root-same-symptom  (P0 over-recall)

    Ranked by symptom cosine: B (0.9) > D (0.8) > C (0.5).
    With k=2, top-2 = {B, D}: B recalled (correct), D recalled (wrong), C missed.
    """
    labels = {
        "A": _label("A", "root1", "sym1"),
        "B": _label("B", "root1", "sym1"),
        "C": _label("C", "root1", "sym2"),
        "D": _label("D", "root2", "sym1"),
    }
    rankings = {"A": ["B", "D", "C"]}  # B(0.9), D(0.8), C(0.5)
    results = {r.quadrant: r for r in recall_at_k_per_quadrant(rankings, labels, k=2)}

    assert results[Q_SAME_ROOT_SAME_SYM].recall_at_k == 1.0  # B in top-2 ✓
    assert results[Q_SAME_ROOT_DIFF_SYM].recall_at_k == 0.0  # C missed (ceiling)
    assert results[Q_DIFF_ROOT_SAME_SYM].recall_at_k == 1.0  # D wrongly in top-2
    assert results[Q_DIFF_ROOT_DIFF_SYM].recall_at_k == 0.0  # no such pair
    assert results[Q_DIFF_ROOT_DIFF_SYM].query_count == 0


def test_recall_at_k_averages_over_queries() -> None:
    """Two queries with different recall@k for the same quadrant: average.

    Query A: 2 same-quad candidates, k=1 -> 1 hit / 2 = 0.5.
    Query D: 1 same-quad candidate,  k=1 -> 1 hit / 1 = 1.0.
    avg = 0.75.
    """
    labels = {
        "A": _label("A", "root1", "sym1"),
        "B": _label("B", "root1", "sym1"),
        "C": _label("C", "root1", "sym1"),
        "D": _label("D", "root1", "sym1"),
        "E": _label("E", "root1", "sym1"),
    }
    rankings = {"A": ["B", "C"], "D": ["E"]}
    results = {r.quadrant: r for r in recall_at_k_per_quadrant(rankings, labels, k=1)}
    assert results[Q_SAME_ROOT_SAME_SYM].recall_at_k == 0.75
    assert results[Q_SAME_ROOT_SAME_SYM].query_count == 2


def test_recall_at_k_k_larger_than_pool() -> None:
    """k larger than the candidate pool -> recall 1.0 for populated quadrants."""
    labels = {
        "A": _label("A", "root1", "sym1"),
        "B": _label("B", "root1", "sym1"),
    }
    rankings = {"A": ["B"]}
    results = {r.quadrant: r for r in recall_at_k_per_quadrant(rankings, labels, k=5)}
    assert results[Q_SAME_ROOT_SAME_SYM].recall_at_k == 1.0


# ── format_quadrant_report ──────────────────────────────────────────


def test_format_quadrant_report_contains_all_quadrants() -> None:
    labels = {"A": _label("A", "root1", "sym1"), "B": _label("B", "root1", "sym1")}
    rankings = {"A": ["B"]}
    results = recall_at_k_per_quadrant(rankings, labels, k=3)
    report = format_quadrant_report(results, k=3)
    for q in (
        Q_SAME_ROOT_SAME_SYM,
        Q_SAME_ROOT_DIFF_SYM,
        Q_DIFF_ROOT_SAME_SYM,
        Q_DIFF_ROOT_DIFF_SYM,
    ):
        assert q in report
    assert "recall@3" in report
