"""Exact-match evaluator — binary checks on category and affected file.

Evaluates whether the Doctor agent:
1. Correctly classified the bug category.
2. Identified the correct affected file.
"""

from __future__ import annotations

import structlog

from benchmark.evaluators.base import BaseEvaluator, EvaluationScore
from benchmark.schema import RunResult
from bug_factory.schema import EvaluationCase

logger = structlog.get_logger(__name__)


class ExactMatchEvaluator(BaseEvaluator):
    """Scores a diagnosis by exact match on category and affected file.

    Scoring (0 or 1):
    * +0.5 if ``result.diagnosis.bug_category`` equals
      ``case.expected.category`` (case-insensitive).
    * +0.5 if ``result.diagnosis.affected_file`` appears in
      ``case.expected.affected_files``.
    * 0.0 if ``result.diagnosis`` is ``None`` or ``result.success`` is ``False``.

    Example:
        >>> evaluator = ExactMatchEvaluator()
        >>> score = await evaluator.evaluate(case, run_result)
        >>> assert score.score in (0.0, 0.5, 1.0)
    """

    name = "exact_match"

    async def evaluate(self, case: EvaluationCase, result: RunResult) -> EvaluationScore:
        if not result.success or result.diagnosis is None:
            return EvaluationScore(
                evaluator=self.name,
                score=0.0,
                reasoning="Run was not successful or diagnosis is missing.",
            )

        diagnosis = result.diagnosis
        reasons: list[str] = []
        score = 0.0

        # ── Check 1: bug category ──────────────────────────────────
        expected_cat = case.expected.category.lower()
        actual_cat = str(diagnosis.get("bug_category", "")).lower()
        category_match = expected_cat == actual_cat

        if category_match:
            score += 0.5
            reasons.append(f"Category matched: {actual_cat}")
        else:
            reasons.append(f"Category mismatch: expected '{expected_cat}', got '{actual_cat}'")

        # ── Check 2: affected file ─────────────────────────────────
        expected_files = {f.lower() for f in case.expected.affected_files}
        actual_file = str(diagnosis.get("affected_file", "")).lower()
        file_match = actual_file in expected_files if expected_files else False

        if file_match:
            score += 0.5
            reasons.append(f"Affected file matched: {actual_file}")
        else:
            if expected_files:
                reasons.append(
                    f"Affected file mismatch: expected one of {expected_files}, got '{actual_file}'"
                )
            else:
                reasons.append("No expected affected files defined; file check skipped.")
                score += 0.5  # No file expectation → full credit for this sub-check

        logger.debug(
            "ExactMatch evaluation",
            case_id=case.case_id,
            score=score,
            category_match=category_match,
            file_match=file_match,
        )

        return EvaluationScore(
            evaluator=self.name,
            score=score,
            reasoning=" | ".join(reasons),
            metadata={
                "category_match": category_match,
                "file_match": file_match,
            },
        )
