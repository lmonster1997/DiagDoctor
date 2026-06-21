"""Keyword-match evaluator — checks fix-keyword coverage in the fix suggestion.

Evaluates how many of the expected fix keywords appear in the Doctor's
``fix_suggestion`` text.
"""

from __future__ import annotations

import re

import structlog

from src.evaluators.base import BaseEvaluator, EvaluationScore
from src.schema import RunResult
from bug_factory.schema import EvaluationCase

logger = structlog.get_logger(__name__)


class KeywordMatchEvaluator(BaseEvaluator):
    """Scores the diagnosis by keyword coverage in ``fix_suggestion``.

    For each keyword in ``case.expected.fix_keywords``, checks whether it
    appears (case-insensitive, whole- or partial-word match) in the
    Doctor's ``fix_suggestion`` text.

    Score = (number of matched keywords) / (total expected keywords).

    Returns 1.0 if there are no expected keywords (nothing to check).

    Example:
        >>> evaluator = KeywordMatchEvaluator()
        >>> score = await evaluator.evaluate(case, run_result)
        >>> print(f"Keyword coverage: {score.score:.0%}")
    """

    name = "keyword_match"

    async def evaluate(self, case: EvaluationCase, result: RunResult) -> EvaluationScore:
        expected_keywords = case.expected.fix_keywords

        if not expected_keywords:
            return EvaluationScore(
                evaluator=self.name,
                score=1.0,
                reasoning="No expected keywords defined.",
            )

        if not result.success or result.diagnosis is None:
            return EvaluationScore(
                evaluator=self.name,
                score=0.0,
                reasoning="Run was not successful or diagnosis is missing.",
            )

        fix_suggestion = str(result.diagnosis.get("fix_suggestion", "")).lower()

        matched: list[str] = []
        missed: list[str] = []

        for keyword in expected_keywords:
            kw_lower = keyword.lower()
            # Use regex word-boundary match for accuracy,
            # fall back to simple substring check for CJK / special chars
            try:
                if re.search(rf"\b{re.escape(kw_lower)}\b", fix_suggestion):
                    matched.append(keyword)
                else:
                    # Simple substring check (handles Chinese chars etc.)
                    if kw_lower in fix_suggestion:
                        matched.append(keyword)
                    else:
                        missed.append(keyword)
            except re.error:
                if kw_lower in fix_suggestion:
                    matched.append(keyword)
                else:
                    missed.append(keyword)

        total = len(expected_keywords)
        score = len(matched) / total

        reasoning_parts: list[str] = []
        if matched:
            reasoning_parts.append(f"Matched: {matched}")
        if missed:
            reasoning_parts.append(f"Missed: {missed}")
        reasoning = " | ".join(reasoning_parts) if reasoning_parts else "No keywords checked."

        logger.debug(
            "KeywordMatch evaluation",
            case_id=case.case_id,
            score=score,
            matched=matched,
            missed=missed,
        )

        return EvaluationScore(
            evaluator=self.name,
            score=score,
            reasoning=reasoning,
            metadata={
                "total_keywords": total,
                "matched_count": len(matched),
                "missed_count": len(missed),
                "matched": matched,
                "missed": missed,
            },
        )
