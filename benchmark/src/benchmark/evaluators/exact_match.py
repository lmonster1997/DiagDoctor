"""Classification evaluator — multi-label P/R/F1 on bug categories.

Evaluates whether the Doctor agent correctly classified the bug:

1. Multi-label precision / recall / F1 on categories.
2. Primary-category hit (the top-ranked category matches expected primary).
3. Cross-layer hit (when expected.cross_layer=True, checks whether the
   diagnosis spans both symptom and root-cause tiers).
"""

from __future__ import annotations

import structlog

from benchmark.evaluators.base import BaseEvaluator, EvaluationScore
from benchmark.schema import RunResult
from bug_factory.schema import EvaluationCase

logger = structlog.get_logger(__name__)


def eval_multi_label(pred: list[str], gold: list[str]) -> dict[str, float]:
    """Compute multi-label precision, recall, and F1.

    Args:
        pred: Predicted category labels.
        gold: Ground-truth category labels.

    Returns:
        Dict with keys ``precision``, ``recall``, ``f1`` (each in [0, 1]).
    """
    p, g = set(pred), set(gold)
    tp = len(p & g)
    precision = tp / len(p) if p else 0.0
    recall = tp / len(g) if g else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


class ClassificationEvaluator(BaseEvaluator):
    """Multi-label classification evaluator (replaces v1 ExactMatchEvaluator).

    Scoring strategy:

    * **Multi-label F1** (weight 0.6): P/R/F1 on ``categories`` sets.
    * **Primary-category hit** (weight 0.2): 1.0 if the diagnosis's
      ``primary_category`` equals the expected ``category``, else 0.0.
    * **Cross-layer hit** (weight 0.2): When ``case.expected.cross_layer``
      is True, 1.0 if ``diagnosis.symptom_tier`` and
      ``diagnosis.root_cause_tier`` match expected; 0.0 otherwise.
      When ``cross_layer`` is False this sub-score is 1.0 (no penalty).

    The overall score = 0.6 * f1 + 0.2 * primary_hit + 0.2 * cross_hit.
    """

    name = "classification"

    async def evaluate(self, case: EvaluationCase, result: RunResult) -> EvaluationScore:
        if not result.success or result.diagnosis is None:
            return EvaluationScore(
                evaluator=self.name,
                score=0.0,
                reasoning="Run was not successful or diagnosis is missing.",
            )

        diagnosis = result.diagnosis
        # V3: structured fields (categories, symptom_tier, etc.) are inside
        # ``report`` (DiagnosisReport). Top-level API fields are mirrors for
        # backward compat, but may be missing for newer fields.
        report = diagnosis.get("report", {}) if isinstance(diagnosis, dict) else {}
        if not isinstance(report, dict):
            report = {}
        reasons: list[str] = []

        # ── Extract predicted categories (multi-label) ──────────────
        # Priority: report.categories > top-level categories > triage scores > primary_category
        pred_categories: list[str] = (
            report.get("categories", [])
            or diagnosis.get("categories", [])
        )
        if not pred_categories:
            triage = diagnosis.get("triage", {})
            scores = triage.get("scores", []) if triage else []
            if scores:
                pred_categories = [
                    s.get("category", "") for s in scores if s.get("confidence", 0) > 0.3
                ]
        if not pred_categories:
            primary = report.get("primary_category", "") or diagnosis.get("primary_category", "")
            if primary:
                pred_categories = [primary]

        # ── Multi-label P/R/F1 ──────────────────────────────────────
        gold_categories = (
            list(case.expected.categories) if case.expected.categories else [case.expected.category]
        )
        ml = eval_multi_label(pred_categories, gold_categories)
        reasons.append(
            f"Multi-label: P={ml['precision']:.2f} R={ml['recall']:.2f} F1={ml['f1']:.2f} "
            f"(pred={pred_categories}, gold={gold_categories})"
        )

        # ── Primary-category hit ────────────────────────────────────
        pred_primary = report.get("primary_category", "") or diagnosis.get("primary_category", "")
        expected_primary = case.expected.category
        primary_hit = 1.0 if pred_primary.lower() == expected_primary.lower() else 0.0
        reasons.append(
            f"Primary: {'HIT' if primary_hit else 'MISS'} "
            f"(expected='{expected_primary}', got='{pred_primary}')"
        )

        # ── Cross-layer hit ─────────────────────────────────────────
        cross_hit = 1.0
        if case.expected.cross_layer:
            pred_symptom = report.get("symptom_tier", "") or diagnosis.get("symptom_tier", "")
            pred_root = report.get("root_cause_tier", "") or diagnosis.get("root_cause_tier", "")
            exp_symptom = case.expected.symptom_tier
            exp_root = case.expected.root_cause_tier
            symptom_ok = pred_symptom == exp_symptom
            root_ok = pred_root == exp_root
            cross_hit = 1.0 if (symptom_ok and root_ok) else 0.5 if (symptom_ok or root_ok) else 0.0
            reasons.append(
                f"Cross-layer: score={cross_hit} "
                f"(symptom: {pred_symptom}=={exp_symptom}={symptom_ok}, "
                f"root: {pred_root}=={exp_root}={root_ok})"
            )
        else:
            reasons.append("Cross-layer: N/A (not a cross-layer case)")

        # ── Overall score ───────────────────────────────────────────
        score = round(0.6 * ml["f1"] + 0.2 * primary_hit + 0.2 * cross_hit, 4)

        logger.debug(
            "Classification evaluation",
            case_id=case.case_id,
            score=score,
            f1=ml["f1"],
            primary_hit=primary_hit,
            cross_hit=cross_hit,
        )

        return EvaluationScore(
            evaluator=self.name,
            score=score,
            reasoning=" | ".join(reasons),
            metadata={
                "precision": ml["precision"],
                "recall": ml["recall"],
                "f1": ml["f1"],
                "primary_category_hit": bool(primary_hit),
                "cross_layer_hit": cross_hit,
                "pred_categories": pred_categories,
                "gold_categories": gold_categories,
            },
        )


# ── Backward-compatible alias ────────────────────────────────────────
ExactMatchEvaluator = ClassificationEvaluator
