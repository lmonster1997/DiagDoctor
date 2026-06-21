"""Base evaluator — abstract interface and shared score model.

Defines :class:`BaseEvaluator` and :class:`EvaluationScore` used by all
concrete evaluator implementations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

from benchmark.src.schema import RunResult
from bug_factory.schema import EvaluationCase


class EvaluationScore(BaseModel):
    """Score produced by a single evaluator for a single case.

    Attributes:
        evaluator: Name of the evaluator (e.g. ``"exact_match"``).
        score: Normalised score in ``[0.0, 1.0]``.
        reasoning: Human-readable explanation of the score.
        metadata: Optional extra data (e.g. matched keywords, thresholds used).
    """

    evaluator: str = ""
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    reasoning: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class BaseEvaluator(ABC):
    """Abstract base class for all benchmark evaluators.

    Subclasses must implement :meth:`evaluate` and set a unique :attr:`name`.

    Typical usage::

        evaluator = ExactMatchEvaluator()
        score = await evaluator.evaluate(case, run_result)
        print(f"{score.evaluator}: {score.score:.2f} — {score.reasoning}")
    """

    name: str = "base"

    @abstractmethod
    async def evaluate(self, case: EvaluationCase, result: RunResult) -> EvaluationScore:
        """Evaluate a single case result.

        Args:
            case: The evaluation case (contains expected diagnosis).
            result: The run result from the Doctor agent (contains actual diagnosis).

        Returns:
            An :class:`EvaluationScore` with a score in ``[0, 1]`` and reasoning.
        """
        ...
