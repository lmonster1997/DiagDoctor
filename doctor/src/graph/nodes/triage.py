"""
TriageAgent node — ⚠️ FULLY DEPRECATED in V3 ⚠️

This module is NO LONGER USED in the DiagDoctor graph. In V3, triage
classification is embedded directly in the ``unified_agent`` System Prompt
(step 1: "理解证据"). Evaluation metrics read ``primary_category`` and
``categories`` from the Agent's JSON output, not from this module.

The file is retained for reference only.  Do NOT import ``triage_node``
or ``route_after_triage`` in new code.

─── V2 legacy documentation ───

TriageAgent node (v2) — multi-label classification + confidence gating.

V2 role: classification + routing (route_after_triage → specialist fan-out).

Key changes from v1:
- Input: NormalizedEvidence (golden_signals + correlations) instead of raw logs/traces
- Output: TriageOutput(scores=list[CategoryScore], primary, cross_layer_suspected)
- Added: confidence gating logic (route_after_triage) — DEPRECATED in V3
- Added: three-tier fallback (gate→retry→general_agent) — removed in V3
"""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from src.config import settings
from src.graph.state import (
    VALID_CATEGORIES,
    CategoryScore,
    DoctorState,
    Finding,
    NormalizedEvidence,
    TriageOutput,
)
from src.knowledge.hybrid_service import get_knowledge_service
from src.prompts.registry import render_prompt

# ── Structured output model (v2 multi-label) ────────────────────────


class _LLMTriageOutput(BaseModel):
    """Internal LLM structured output — maps to TriageOutput."""

    scores: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of {category, confidence} dicts",
    )
    primary: str = Field(default="", description="Highest-confidence category")
    reasoning: str = Field(default="")
    cross_layer_suspected: bool = Field(
        default=False,
        description="Whether this bug likely spans frontend+backend",
    )


# ── Formatting helpers (use normalized evidence) ────────────────────


def _format_golden_signals(evidence: NormalizedEvidence) -> str:
    """Format golden signals for the LLM prompt."""
    if not evidence.golden_signals:
        return "（无关键信号）"

    lines: list[str] = []
    for sig in evidence.golden_signals[:30]:
        tier_label = "🖥前端" if sig.service_tier == "frontend" else "🖧后端"
        sev_label = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}.get(sig.severity, "•")
        lines.append(f"{sev_label} [{tier_label}] [{sig.source}] {sig.summary}")
    return "\n".join(lines)


def _format_correlations(evidence: NormalizedEvidence) -> str:
    """Format cross-layer correlations for the LLM prompt."""
    if not evidence.correlations:
        return "（无跨层关联）"

    lines: list[str] = []
    for corr in evidence.correlations[:10]:
        lines.append(
            f"- [{corr.correlation_id}] {corr.description} "
            f"(trace={corr.trace_id}, confidence={corr.confidence:.1f})"
        )
    return "\n".join(lines)


def _format_similar_cases(cases: list[dict[str, Any]]) -> str:
    """Format similar historical cases."""
    if not cases:
        return "（无类似历史案例）"

    lines: list[str] = []
    for i, case in enumerate(cases, 1):
        similarity = case.get("similarity_score", 0.0)
        lines.append(
            f"  {i}. [相似度: {similarity:.2f}] "
            f"类别: {case.get('category', 'N/A')} | "
            f"根因: {case.get('root_cause', 'N/A')}"
        )
    return "\n".join(lines)


# ── Confidence gating function ──────────────────────────────────────


def route_after_triage(state: DoctorState) -> list[str]:
    """
    ⚠️ FULLY DEPRECATED in V3 — DO NOT USE in new graph topologies.

    Determine which specialist agents to activate based on triage confidence.

    V3 uses a linear topology (ingest → unified_agent → reporter) without
    specialist fan-out. This function is retained for backward compatibility
    and reference only.

    V2 Gate strategy (from-scratch §5.5):
    1. primary confidence < 0.5 → only general_agent (full-toolset fallback)
    2. cross_layer_suspected or 2nd category > 0.4 → fan-out two specialists
    3. Otherwise → single specialist

    Returns:
        List of node names to fan out to (LangGraph runs them in parallel).
    """
    triage = state.triage
    if not triage.scores:
        return ["general_agent"]

    # Sort by confidence descending
    sorted_scores = sorted(triage.scores, key=lambda s: -s.confidence)
    top = sorted_scores[0]
    second = sorted_scores[1] if len(sorted_scores) > 1 else None

    # Low confidence → full fallback
    if top.confidence < 0.5:
        return ["general_agent"]

    # Map category to specialist node name
    category_to_specialist: dict[str, str] = {
        "frontend_crash": "frontend_specialist",
        "backend_error": "backend_specialist",
        "performance": "perf_specialist",
        "logic": "logic_specialist",
        "data": "logic_specialist",
        "config": "backend_specialist",
    }

    targets: list[str] = []
    primary_node = category_to_specialist.get(top.category, "backend_specialist")
    targets.append(primary_node)

    # Cross-layer or high-confidence second → fan out two
    if triage.cross_layer_suspected or (second and second.confidence > 0.4):
        if second:
            second_node = category_to_specialist.get(second.category, "backend_specialist")
            if second_node not in targets:
                targets.append(second_node)

        # When cross_layer_suspected, ensure both frontend and backend tiers
        # are covered — the second-highest category may not be the "other"
        # tier (e.g. LLM may assign ``data`` over ``backend_error`` for a
        # missing-fields root cause).  Fan out the opposite tier explicitly
        # so the specialist on the root-cause side always gets a chance.
        if triage.cross_layer_suspected:
            if "frontend_specialist" in targets and "backend_specialist" not in targets:
                targets.append("backend_specialist")
            elif "backend_specialist" in targets and "frontend_specialist" not in targets:
                targets.append("frontend_specialist")

    return list(dict.fromkeys(targets))  # dedup preserving order


# ── Main node function (v2) ─────────────────────────────────────────


async def triage_node(state: DoctorState) -> dict[str, Any]:
    """
    ⚠️ FULLY DEPRECATED in V3 — NOT registered in the V3 graph.

    Multi-label triage node: analyze normalized evidence → TriageOutput.

    In V3, this node is replaced by the unified_agent's System Prompt
    (step 1: classification analysis). Evaluation reads ``primary_category``
    and ``categories`` from the Agent's JSON output directly.

    Uses the NormalizedEvidence produced by the Ingest layer (golden_signals,
    correlations, timeline) — NOT raw logs/traces directly.

    Workflow:
    1. Format golden_signals + correlations for prompt context
    2. RAG retrieve similar historical cases
    3. Render triage_v2.j2 template
    4. Call LLM with structured output (TriageOutput)
    5. Return updated triage + findings

    Args:
        state: Current DoctorState with evidence (NormalizedEvidence).

    Returns:
        Dict with 'triage' (TriageOutput with primary + scores=categories)
        and 'findings' to merge into state.
    """
    evidence: NormalizedEvidence = state.evidence

    # 1. Format normalized evidence for prompt
    signals_text = _format_golden_signals(evidence)
    correlations_text = _format_correlations(evidence)

    # 2. RAG retrieve similar historical cases
    knowledge_service = get_knowledge_service()
    similar = await knowledge_service.search_historical_cases(evidence.user_report, k=3)
    similar_text = _format_similar_cases(similar)

    # 3. Render prompt (v2 template with normalized evidence context)
    prompt = render_prompt(
        "triage_v2.j2",
        user_report=evidence.user_report or "（无用户报告）",
        golden_signals=signals_text,
        correlations=correlations_text,
        similar_cases=similar_text,
        frontend_span_count=str(evidence.frontend_span_count),
        backend_span_count=str(evidence.backend_span_count),
        noise_ratio=f"{evidence.noise_ratio:.1%}",
    )

    # 4. Call LLM with structured output
    llm = ChatOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        temperature=settings.llm_temperature,
    )

    try:
        structured_llm = llm.with_structured_output(_LLMTriageOutput)
        raw_output = await structured_llm.ainvoke(prompt)
        raw_dict = raw_output if isinstance(raw_output, dict) else raw_output.model_dump()

        scores_raw: list[dict[str, Any]] = raw_dict.get("scores", [])
        primary = str(raw_dict.get("primary", "")).strip().lower()
        reasoning = str(raw_dict.get("reasoning", ""))
        cross_layer = bool(raw_dict.get("cross_layer_suspected", False))

        # Build CategoryScore list
        scores: list[CategoryScore] = []
        for s in scores_raw:
            cat = str(s.get("category", "")).strip().lower()
            if cat in VALID_CATEGORIES:
                conf = float(s.get("confidence", 0.0))
                scores.append(CategoryScore(category=cat, confidence=conf))

        # Fallback: if no valid scores, create one from primary
        if not scores:
            primary = primary if primary in VALID_CATEGORIES else "backend_error"
            scores.append(CategoryScore(category=primary, confidence=0.5))

        # Ensure primary is set
        if primary not in VALID_CATEGORIES:
            primary = scores[0].category if scores else "backend_error"

        triage_output = TriageOutput(
            scores=scores,
            primary=primary,
            reasoning=reasoning,
            cross_layer_suspected=cross_layer,
        )

        finding = Finding(
            agent="TriageAgent",
            summary=f"Multi-label triage: primary={primary}, "
            f"cross_layer={cross_layer}, "
            f"scores={[(s.category, f'{s.confidence:.2f}') for s in scores]}",
            confidence=scores[0].confidence if scores else 0.5,
        )

    except Exception:
        # Fallback: unstructured call
        response = await llm.ainvoke(prompt)
        content = str(response.content).strip().lower()

        # Find primary category
        primary = "backend_error"
        for valid_cat in VALID_CATEGORIES:
            if valid_cat in content:
                primary = valid_cat
                break

        scores = [CategoryScore(category=primary, confidence=0.5)]
        triage_output = TriageOutput(
            scores=scores,
            primary=primary,
            reasoning=f"Fallback classification: {content[:200]}",
            cross_layer_suspected=False,
        )

        finding = Finding(
            agent="TriageAgent",
            summary=f"Fallback triage: primary={primary}",
            confidence=0.3,
        )

    return {
        "triage": triage_output,
        "findings": [finding],
    }
