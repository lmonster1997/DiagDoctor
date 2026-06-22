"""LLM Judge evaluator — uses a strong LLM to score diagnosis correctness.

Evaluates the Doctor agent's diagnosis by asking an impartial LLM judge
to assess correctness based on the evaluation case's criteria.  Includes
a built-in cache to avoid re-evaluating identical (case_id, diagnosis)
pairs.

Typical usage::

    from langchain_openai import ChatOpenAI
    judge_llm = ChatOpenAI(model="gpt-4o", temperature=0.0)
    evaluator = LLMJudgeEvaluator(judge_llm)
    score = await evaluator.evaluate(case, run_result)
    print(f"LLM Judge: {score.score:.2f} — {score.reasoning}")
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import structlog
from pydantic import BaseModel, Field

from benchmark.evaluators.base import BaseEvaluator, EvaluationScore
from benchmark.schema import RunResult
from bug_factory.schema import EvaluationCase

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Structured output model for the LLM judge
# ---------------------------------------------------------------------------


class JudgeResponse(BaseModel):
    """Structured output the LLM judge must return.

    Attributes:
        score: Correctness score in ``[0.0, 1.0]``.
        reasoning: Detailed explanation of the score, referencing specific
            aspects of the diagnosis versus the expected criteria.
    """

    score: float = Field(default=0.0, ge=0.0, le=1.0)
    reasoning: str = Field(default="", description="Detailed justification for the score")


# ---------------------------------------------------------------------------
# LLM Judge Evaluator
# ---------------------------------------------------------------------------


class LLMJudgeEvaluator(BaseEvaluator):
    """Evaluates diagnosis correctness using an impartial LLM judge.

    The judge LLM receives:

    1. The user's original bug report (from ``case.input.user_report``).
    2. The expected diagnosis criteria (from ``case.expected``).
    3. The Doctor agent's actual diagnosis (from ``result.diagnosis``).
    4. The LLM judge criteria text (from ``case.expected.llm_judge_criteria``).

    It is instructed to return a score ``[0, 1]`` and detailed reasoning.

    **Caching**: Results are cached keyed on ``(case_id, sha256(diagnosis))``
    to avoid redundant (and costly) LLM calls when re-running evaluations.

    Args:
        judge_llm: A LangChain chat model instance (e.g. ``ChatOpenAI(model="gpt-4o")``).
            This should typically be a stronger model than the Doctor agent
            uses, to provide a reliable evaluation.
        cache_enabled: Whether to enable the in-memory result cache (default ``True``).

    Example:
        >>> from langchain_openai import ChatOpenAI
        >>> judge = ChatOpenAI(model="gpt-4o", temperature=0.0)
        >>> evaluator = LLMJudgeEvaluator(judge)
        >>> score = await evaluator.evaluate(case, result)
        >>> assert 0.0 <= score.score <= 1.0
    """

    name = "llm_judge_correctness"

    def __init__(self, judge_llm: Any, cache_enabled: bool = True) -> None:
        self.llm = judge_llm
        self.cache_enabled = cache_enabled
        self._cache: dict[str, EvaluationScore] = {}

    # ── Public API ──────────────────────────────────────────────────

    async def evaluate(self, case: EvaluationCase, result: RunResult) -> EvaluationScore:
        """Evaluate a single case using the LLM judge.

        Returns a score of ``0.0`` (with reasoning) if the run failed or
        no diagnosis is available, without calling the LLM.
        """
        if not result.success or result.diagnosis is None:
            return EvaluationScore(
                evaluator=self.name,
                score=0.0,
                reasoning="Run was not successful or diagnosis is missing.",
            )

        # ── Cache check ─────────────────────────────────────────────
        cache_key = self._build_cache_key(case.case_id, result.diagnosis)
        if self.cache_enabled and cache_key in self._cache:
            cached = self._cache[cache_key]
            logger.debug(
                "LLM Judge cache hit",
                case_id=case.case_id,
                cached_score=cached.score,
            )
            return cached

        # ── Build prompt and invoke LLM ─────────────────────────────
        prompt = self._build_prompt(case, result)

        try:
            structured_llm = self.llm.with_structured_output(JudgeResponse)
            raw_output = await structured_llm.ainvoke(prompt)

            # Normalise: structured output may return a dict or a JudgeResponse
            if isinstance(raw_output, dict):
                judge_response = JudgeResponse.model_validate(raw_output)
            else:
                judge_response = raw_output

        except Exception:
            logger.exception(
                "LLM Judge structured output failed, falling back to unstructured",
                case_id=case.case_id,
            )
            try:
                # Fallback: unstructured call, then parse manually
                raw_text = await self.llm.ainvoke(prompt)
                judge_response = self._parse_unstructured_response(raw_text)
            except Exception:
                logger.exception(
                    "LLM Judge fallback also failed, returning 0.0",
                    case_id=case.case_id,
                )
                return EvaluationScore(
                    evaluator=self.name,
                    score=0.0,
                    reasoning="LLM Judge failed to produce a valid response.",
                )

        # Clamp score to valid range
        score = max(0.0, min(1.0, judge_response.score))

        eval_score = EvaluationScore(
            evaluator=self.name,
            score=score,
            reasoning=judge_response.reasoning,
            metadata={
                "judge_model": getattr(self.llm, "model_name", "unknown"),
                "cache_key": cache_key,
            },
        )

        # ── Store in cache ──────────────────────────────────────────
        if self.cache_enabled:
            self._cache[cache_key] = eval_score

        logger.info(
            "LLM Judge evaluation complete",
            case_id=case.case_id,
            score=score,
        )

        return eval_score

    # ── Internal helpers ────────────────────────────────────────────

    @staticmethod
    def _build_cache_key(case_id: str, diagnosis: dict[str, Any]) -> str:
        """Build a deterministic cache key from case_id and diagnosis content."""
        diagnosis_json = json.dumps(diagnosis, sort_keys=True, ensure_ascii=False, default=str)
        diagnosis_hash = hashlib.sha256(diagnosis_json.encode("utf-8")).hexdigest()[:16]
        return f"{case_id}:{diagnosis_hash}"

    @staticmethod
    def _build_prompt(case: EvaluationCase, result: RunResult) -> str:
        """Build the judge prompt with all required context sections.

        Sections:
        1. Case description (user_report)
        2. Expected diagnosis criteria
        3. Doctor agent's actual diagnosis
        4. Scoring criteria
        5. Output format instruction
        """
        user_report = case.input.user_report
        expected = case.expected
        diagnosis = result.diagnosis or {}

        # Format the diagnosis nicely for the judge
        diagnosis_json = json.dumps(diagnosis, indent=2, ensure_ascii=False, default=str)

        # Build expected criteria summary
        expected_lines: list[str] = []
        expected_lines.append(f"- 类别: {expected.category}")
        expected_lines.append(f"- 根因摘要: {expected.root_cause_summary}")
        if expected.affected_files:
            expected_lines.append(f"- 受影响文件: {', '.join(expected.affected_files)}")
        if expected.fix_keywords:
            expected_lines.append(f"- 修复关键词: {', '.join(expected.fix_keywords)}")

        expected_summary = "\n".join(expected_lines)

        prompt = f"""你是一个 Bug 诊断评测专家。请根据以下信息，对 AI 诊断助手（Doctor）的诊断结果进行评分。

---

## 1. 用户报告

{user_report}

---

## 2. 预期诊断标准

{expected_summary}

---

## 3. Doctor 的诊断结果

```json
{diagnosis_json}
```

---

## 4. 评分标准

{expected.llm_judge_criteria}

---

## 5. 评分要求

请综合以上信息，给出一个 0 到 1 之间的分数，并附上详细的评分理由。

**评分指南：**
- **1.0**：诊断完全正确，根因、受影响文件、修复建议均与预期一致。
- **0.7-0.9**：诊断基本正确，主要问题识别准确，但细节略有偏差或遗漏。
- **0.4-0.6**：部分正确，识别了部分问题但未完整定位根因。
- **0.1-0.3**：诊断方向有误，只触及了表面现象，未找到真正的根因。
- **0.0**：诊断完全错误或无关。

请输出 JSON 格式：
```json
{{
  "score": <0.0 到 1.0 之间的浮点数>,
  "reasoning": "<详细的评分理由>"
}}
```"""

        return prompt

    @staticmethod
    def _parse_unstructured_response(raw_text: Any) -> JudgeResponse:
        """Attempt to parse a JSON JudgeResponse from unstructured LLM text output.

        Tries multiple strategies:
        1. Parse the whole content as JSON.
        2. Extract the first JSON object from the text.

        Args:
            raw_text: The raw response from the LLM (AIMessage or str).

        Returns:
            A :class:`JudgeResponse` if parsing succeeds.

        Raises:
            ValueError: If no valid JSON with ``score`` can be extracted.
        """
        import re

        content: str
        # Handle langchain AIMessage
        if hasattr(raw_text, "content"):
            content = str(raw_text.content)
        else:
            content = str(raw_text)

        # Strategy 1: Try direct JSON parse
        try:
            data = json.loads(content)
            return JudgeResponse.model_validate(data)
        except (json.JSONDecodeError, ValueError):
            pass

        # Strategy 2: Find JSON object in the text
        json_match = re.search(r"\{[^{}]*\}", content, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
                return JudgeResponse.model_validate(data)
            except (json.JSONDecodeError, ValueError):
                pass

        # Strategy 3: Try to find "score" key with a broader pattern
        broad_match = re.search(
            r"\{[^}]*\"score\"[^}]*\"reasoning\"[^}]*\}",
            content,
            re.DOTALL,
        )
        if broad_match:
            try:
                data = json.loads(broad_match.group(0))
                return JudgeResponse.model_validate(data)
            except (json.JSONDecodeError, ValueError):
                pass

        raise ValueError(f"Could not parse JudgeResponse from LLM output: {content[:500]}")

    def clear_cache(self) -> None:
        """Clear the in-memory evaluation cache."""
        self._cache.clear()
        logger.info("LLM Judge cache cleared")

    @property
    def cache_size(self) -> int:
        """Return the number of cached evaluation results."""
        return len(self._cache)
