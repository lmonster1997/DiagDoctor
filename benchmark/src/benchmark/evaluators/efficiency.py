"""Efficiency evaluator — scores the resource efficiency of a diagnosis run.

Evaluates how efficiently the Doctor agent reached its diagnosis,
penalising excessive tool calls, token usage, and latency.
"""

from __future__ import annotations

import math

import structlog

from benchmark.evaluators.base import BaseEvaluator, EvaluationScore
from benchmark.schema import RunResult
from bug_factory.schema import EvaluationCase

logger = structlog.get_logger(__name__)

# ── Default thresholds (conservative, tuned later) ──────────────────
_DEFAULT_MAX_TOOL_CALLS = 20  # tool_calls beyond this → full penalty
_DEFAULT_MAX_TOKENS = 50_000  # total tokens beyond this → full penalty
_DEFAULT_MAX_LATENCY_MS = 120_000  # 2 min latency → full penalty


class EfficiencyEvaluator(BaseEvaluator):
    """Scores the resource efficiency of the diagnosis.

    The score is a weighted average of three sub-scores:

    * **tool_calls** (weight 0.3): Fewer tool calls → higher score.
    * **tokens** (weight 0.3): Fewer total tokens → higher score.
    * **latency** (weight 0.4): Lower latency → higher score.

    Each sub-score uses an exponential decay: ``exp(-actual / threshold)``,
    clamped to ``[0, 1]``.

    If ``result`` is not successful, returns 0.0.

    Attributes:
        max_tool_calls: Tool-call count at which the sub-score reaches ~0.37.
        max_tokens: Token count at which the sub-score reaches ~0.37.
        max_latency_ms: Latency (ms) at which the sub-score reaches ~0.37.
    """

    name = "efficiency"

    def __init__(
        self,
        max_tool_calls: int = _DEFAULT_MAX_TOOL_CALLS,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        max_latency_ms: float = _DEFAULT_MAX_LATENCY_MS,
    ) -> None:
        self.max_tool_calls = max_tool_calls
        self.max_tokens = max_tokens
        self.max_latency_ms = max_latency_ms

    async def evaluate(self, case: EvaluationCase, result: RunResult) -> EvaluationScore:
        if not result.success:
            return EvaluationScore(
                evaluator=self.name,
                score=0.0,
                reasoning="Run was not successful.",
            )

        meta = result.metadata
        tool_calls = meta.tool_calls
        total_tokens = sum(meta.token_usage.values()) if meta.token_usage else 0
        latency_ms = meta.latency_ms

        # ── Sub-scores (exponential decay) ─────────────────────────
        tool_score = self._decay(tool_calls, self.max_tool_calls)
        token_score = self._decay(total_tokens, self.max_tokens)
        latency_score = self._decay(latency_ms, self.max_latency_ms)

        # Weighted average
        score = 0.3 * tool_score + 0.3 * token_score + 0.4 * latency_score
        score = round(score, 4)

        reasoning = (
            f"tool_calls={tool_calls} (score={tool_score:.2f}), "
            f"tokens={total_tokens} (score={token_score:.2f}), "
            f"latency={latency_ms:.0f}ms (score={latency_score:.2f})"
        )

        logger.debug(
            "Efficiency evaluation",
            case_id=case.case_id,
            score=score,
            tool_calls=tool_calls,
            tokens=total_tokens,
            latency_ms=latency_ms,
        )

        return EvaluationScore(
            evaluator=self.name,
            score=score,
            reasoning=reasoning,
            metadata={
                "tool_calls": tool_calls,
                "total_tokens": total_tokens,
                "latency_ms": latency_ms,
                "tool_score": round(tool_score, 4),
                "token_score": round(token_score, 4),
                "latency_score": round(latency_score, 4),
            },
        )

    @staticmethod
    def _decay(actual: float, threshold: float) -> float:
        """Exponential decay: ``exp(-actual / threshold)``, clamped to [0, 1]."""
        if actual <= 0:
            return 1.0
        if threshold <= 0:
            return 0.0
        raw = math.exp(-actual / threshold)
        return max(0.0, min(1.0, raw))
