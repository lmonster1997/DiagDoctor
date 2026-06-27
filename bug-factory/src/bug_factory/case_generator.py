"""
Case Generator — produces evaluation cases from bug-factory pipeline results.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
import yaml

from bug_factory.schema import (
    CollectedEvidence,
    EvaluationCase,
    EvaluationCaseExpected,
    EvaluationCaseInput,
)

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

    from bug_factory.schema import BugRecipe, InjectionResult, TriggerResult

logger = structlog.get_logger(__name__)

# Regex to extract new-file line number from unified diff hunk header.
# Example: "@@ -10,6 +10,8 @@ def some_function():" → group(1)="10"
_DIFF_HUNK_RE = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@")

_USER_REPORT_PROMPT = """\
你是一个普通用户，正在使用 TaskFlow 任务管理应用。

请根据以下技术性的 Bug 标题，用自然的语言描述你遇到问题的过程和感受，
就好像你在向技术支持人员报告问题一样。

要求：
1. 使用第一人称（"我"）
2. 描述具体操作和观察到的现象
3. 不要使用任何技术术语
4. 语气自然，2-4 句话

Bug 标题：{title}

用户报告："""


class CaseGenerator:
    """Generate evaluation cases from bug-factory pipeline results."""

    def __init__(self, llm: BaseChatModel | None = None, output_dir: Path | None = None) -> None:
        self.llm = llm
        if output_dir is None:
            output_dir = (
                Path(__file__).resolve().parent.parent.parent.parent / "bug-factory" / "output"
            )
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def generate(
        self,
        recipe: BugRecipe,
        injection_result: InjectionResult,
        trigger_result: TriggerResult,
        evidence: CollectedEvidence,
    ) -> EvaluationCase:
        logger.info("Generating evaluation case", recipe_id=recipe.id)
        generated_at = datetime.now(timezone.utc).isoformat()  # noqa: UP017
        user_report = await self._make_user_report(recipe)
        summary = self._make_trigger_summary(trigger_result)
        evidence_files = {
            "logs_file": "evidence/logs.json",
            "traces_file": "evidence/traces.json",
            "browser_errors_file": "evidence/browser_errors.json",
        }

        # ── Derive cross_layer / symptom_tier / root_cause_tier ──────
        cross_layer = False
        symptom_tier = "backend"
        root_cause_tier = "backend"
        if recipe.trigger.expected_evidence is not None:
            ee = recipe.trigger.expected_evidence
            symptom_tier = ee.symptom_tier
            root_cause_tier = ee.root_cause_tier
            # Cross-layer: symptom is frontend but root cause is backend/data
            cross_layer = symptom_tier == "frontend" and root_cause_tier in ("backend", "data")

        # ── Extract affected_line from git diff ──────────────────────
        affected_line = (
            self._extract_line_from_diff(
                injection_result.diff, recipe.expected_diagnosis.affected_file
            )
            or recipe.expected_diagnosis.affected_line
        )
        if affected_line:
            logger.debug(
                "Extracted affected_line from git diff",
                recipe_id=recipe.id,
                affected_line=affected_line,
            )

        # ── Derive retrieval_gold from recipe ─────────────────────────
        retrieval_gold = self._derive_retrieval_gold(recipe)

        # ── Derive categories (multi-label) ───────────────────────────
        categories: list[str] = list(recipe.categories) if recipe.categories else [recipe.category]

        # ── Compute noise_ratio ───────────────────────────────────────
        noise_ratio = self._compute_noise_ratio(evidence)

        expected = EvaluationCaseExpected(
            category=recipe.category,
            categories=categories,
            root_cause_summary=recipe.expected_diagnosis.root_cause,
            affected_files=(
                [recipe.expected_diagnosis.affected_file]
                if recipe.expected_diagnosis.affected_file
                else []
            ),
            fix_keywords=recipe.evaluation.must_mention_keywords,
            llm_judge_criteria=recipe.evaluation.llm_judge_criteria,
            cross_layer=cross_layer,
            symptom_tier=symptom_tier,
            root_cause_tier=root_cause_tier,
            noise_ratio=noise_ratio,
            retrieval_gold=retrieval_gold,
        )
        case = EvaluationCase(
            case_id=recipe.id,
            generated_at=generated_at,
            recipe_id=recipe.id,
            input=EvaluationCaseInput(
                user_report=user_report,
                evidence=evidence_files,
                trigger_summary=summary,
            ),
            expected=expected,
        )
        self._save_case(case)
        return case

    async def _make_user_report(self, recipe: BugRecipe) -> str:
        if self.llm is not None:
            return await self._ai_report(recipe.title)
        hints = {
            "performance": "操作变得非常慢，等了很久才有响应",
            "backend_error": "操作时出现了错误提示",
            "frontend_crash": "页面突然崩溃/白屏",
            "logic": "数据显示不正确",
            "data": "数据丢失或不一致",
            "config": "功能无法正常使用",
        }
        hint = hints.get(recipe.category, "操作出现了异常")
        return f"我在使用 TaskFlow 时遇到了问题。{recipe.title}。{hint}，请帮我排查一下。"

    async def _ai_report(self, title: str) -> str:
        from langchain_core.messages import HumanMessage

        try:
            resp = await self.llm.ainvoke(  # type: ignore[union-attr]
                [HumanMessage(content=_USER_REPORT_PROMPT.format(title=title))]
            )
            text = str(resp.content).strip()
            if text:
                return text
        except Exception as exc:
            logger.warning("AI user report failed", error=str(exc))
        return f"我在使用 TaskFlow 时遇到问题：{title}。操作变得很慢/报错了，请帮我看看。"

    def _make_trigger_summary(self, result: TriggerResult) -> str:
        total = len(result.steps)
        ok = sum(1 for s in result.steps if s.success)
        lines = [f"Trigger: {ok}/{total} steps succeeded"]
        if result.error:
            lines.append(f"Error: {result.error}")
        for i, step in enumerate(result.steps):
            status = "✓" if step.success else "✗"
            ms = f"{step.elapsed_ms:.0f}ms" if step.elapsed_ms else "N/A"
            err = f" — {step.error}" if step.error else ""
            lines.append(f"  [{status}] Step {i} ({step.action}) {ms}{err}")
        return "\n".join(lines)

    def _save_case(self, case: EvaluationCase) -> None:
        case_dir = self.output_dir / case.case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        path = case_dir / "case.yaml"
        body = yaml.dump(
            case.model_dump(), allow_unicode=True, sort_keys=False, default_flow_style=False
        )
        header = (
            f"# Evaluation case generated by bug-factory\n"
            f"# Case ID: {case.case_id}\n"
            f"# Generated at: {case.generated_at}\n"
            f"# Recipe: {case.recipe_id}\n\n"
        )
        path.write_text(header + body, encoding="utf-8")
        logger.info("Evaluation case saved", path=str(path))

    # ── Diff parsing ─────────────────────────────────────────────────

    @staticmethod
    def _extract_line_from_diff(diff: str, target_file: str) -> int | None:
        """Extract the first added line number for *target_file* from a unified diff.

        Parses hunk headers like ``@@ -10,6 +10,8 @@`` to get the new-file
        starting line number (the ``+10`` part).  Returns the first such
        line found, or ``None`` if the diff could not be parsed.
        """
        if not diff:
            return None
        target_marker = target_file.replace("\\", "/")
        in_target = target_marker in diff.replace("\\", "/")
        for line in diff.split("\n"):
            # Switch into target-file section when we see its header.
            if line.startswith("+++ ") and not in_target:
                in_target = target_marker in line.replace("\\", "/")
                continue
            if not in_target:
                continue
            m = _DIFF_HUNK_RE.match(line)
            if m:
                return int(m.group(1))
        return None

    # ── retrieval_gold derivation ────────────────────────────────────

    @staticmethod
    def _derive_retrieval_gold(recipe: BugRecipe) -> RetrievalGold | None:  # type: ignore[name-defined]  # noqa: F821
        """Derive retrieval gold-standard from the recipe."""
        from bug_factory.schema import RetrievalGold

        diag = recipe.expected_diagnosis
        # If recipe already specifies retrieval_gold, use it directly.
        if diag.retrieval_gold is not None:
            return diag.retrieval_gold
        # Otherwise build a minimal gold from affected_files.
        chunks: list[str] = []
        if diag.affected_file:
            # Use file::function notation for code chunks.
            base = diag.affected_file.replace("\\", "/")
            chunks.append(f"{base}::{diag.root_cause[:40]}")
        return (
            RetrievalGold(
                code_chunks=chunks,
                evidence_ids=[],
                similar_cases=[],
            )
            if chunks
            else None
        )

    # ── noise_ratio ──────────────────────────────────────────────────

    @staticmethod
    def _compute_noise_ratio(evidence: CollectedEvidence) -> float:
        """Compute the noise ratio of collected evidence.

        Returns a value in [0, 1] where higher values mean more noise.
        Simplified heuristic: ratio of non-error log lines to total.
        """
        total = len(evidence.logs)
        if total == 0:
            return 0.0
        error_keywords = ("error", "exception", "traceback", "fail", "500", "critical")
        signal_count = sum(
            1 for e in evidence.logs if any(kw in e.line.lower() for kw in error_keywords)
        )
        noise = total - signal_count
        return round(noise / total, 2)
