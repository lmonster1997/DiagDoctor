"""
TriageAgent node — classifies bug category from evidence.

Implements the first step of the diagnosis pipeline:
1. Summarize logs and traces from evidence
2. RAG retrieve similar historical cases
3. Call LLM with structured output to classify bug category

The output is a TriageOutput with category, confidence, and reasoning.
"""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from src.config import settings
from src.graph.state import DoctorState, Evidence, Finding, LogEntry, TraceSpan
from src.knowledge.hybrid_service import get_knowledge_service
from src.prompts.registry import render_prompt

# ── Structured output model ─────────────────────────────────────────


class TriageOutput(BaseModel):
    """Structured output from the triage LLM call."""

    category: str = Field(description="Bug category classification")
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    reasoning: str = Field(default="", description="Brief reasoning for the classification")


# ── Valid categories ─────────────────────────────────────────────────

VALID_CATEGORIES: frozenset[str] = frozenset(
    {
        "frontend_crash",
        "backend_error",
        "performance",
        "logic",
        "data",
        "config",
    }
)

# ── Summarization helpers ───────────────────────────────────────────


def summarize_logs(logs: list[LogEntry], max_entries: int = 50) -> str:
    """
    Summarize log entries into a compact string for the prompt.

    Prioritizes ERROR/WARNING level logs and deduplicates repeated messages.

    Args:
        logs: List of LogEntry objects from evidence.
        max_entries: Maximum number of entries to include.

    Returns:
        Formatted log summary string.
    """
    if not logs:
        return "（无日志数据）"

    # Sort by severity: ERROR > WARNING > INFO > DEBUG
    severity_order = {"ERROR": 0, "WARNING": 1, "INFO": 2, "DEBUG": 3}

    sorted_logs = sorted(logs, key=lambda entry: severity_order.get(entry.level.upper(), 4))
    truncated = sorted_logs[:max_entries]

    lines: list[str] = []
    for entry in truncated:
        ts = entry.timestamp.isoformat() if entry.timestamp else "N/A"
        lines.append(f"[{ts}] [{entry.level}] [{entry.service}] {entry.message}")

    return "\n".join(lines)


def summarize_traces(
    traces: list[TraceSpan],
    max_entries: int = 50,
    slow_threshold_ms: float = 200.0,
) -> str:
    """
    Summarize trace spans into a compact string for the prompt.

    Prioritizes error spans and slow spans above the threshold.

    Args:
        traces: List of TraceSpan objects from evidence.
        max_entries: Maximum number of entries to include.
        slow_threshold_ms: Threshold in ms above which a span is considered slow.

    Returns:
        Formatted trace summary string.
    """
    if not traces:
        return "（无 Trace 数据）"

    # Separate error and slow spans
    error_spans = [t for t in traces if t.status == "error"]
    slow_spans = [t for t in traces if t.status != "error" and t.duration_ms >= slow_threshold_ms]
    other_spans = [t for t in traces if t.status != "error" and t.duration_ms < slow_threshold_ms]

    prioritized = error_spans + slow_spans + other_spans
    truncated = prioritized[:max_entries]

    lines: list[str] = []
    for span in truncated:
        status_mark = (
            "❌"
            if span.status == "error"
            else ("🐢" if span.duration_ms >= slow_threshold_ms else "✓")
        )
        lines.append(
            f"{status_mark} [{span.service}] {span.name} "
            f"({span.duration_ms:.1f}ms, status={span.status})"
        )

    return "\n".join(lines)


def format_similar_cases(cases: list[dict[str, Any]]) -> str:
    """
    Format similar historical cases for display in the prompt.

    Args:
        cases: List of dicts from KnowledgeService.search_historical_cases.

    Returns:
        Formatted string of similar cases.
    """
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


# ── Main node function ──────────────────────────────────────────────


async def triage_node(state: DoctorState) -> dict[str, Any]:
    """
    Triage node: analyze evidence and classify bug category.

    Workflow:
    1. Summarize logs and traces from evidence
    2. RAG retrieve similar historical cases from knowledge base
    3. Render prompt template with all context
    4. Call LLM with structured output (TriageOutput)
    5. Return updated bug_category, findings

    Args:
        state: Current DoctorState with evidence.

    Returns:
        Dict with keys 'bug_category' and 'findings' to merge into state.
    """
    evidence: Evidence = state.evidence

    # 1. Summarize evidence
    logs_summary = summarize_logs(evidence.logs)
    traces_summary = summarize_traces(evidence.traces)

    # 2. RAG retrieve similar historical cases
    knowledge_service = get_knowledge_service()
    similar = await knowledge_service.search_historical_cases(evidence.user_report, k=3)
    similar_text = format_similar_cases(similar)

    # 3. Render the prompt
    prompt = render_prompt(
        "triage.j2",
        user_report=evidence.user_report or "（无用户报告）",
        logs_summary=logs_summary,
        traces_summary=traces_summary,
        similar_cases=similar_text,
    )

    # 4. Call LLM with structured output
    llm = ChatOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        temperature=settings.llm_temperature,
    )

    try:
        structured_llm = llm.with_structured_output(TriageOutput)
        raw_output = await structured_llm.ainvoke(prompt)
        triage_output = (
            TriageOutput.model_validate(raw_output) if isinstance(raw_output, dict) else raw_output
        )

        category = triage_output.category.strip().lower()
        if category not in VALID_CATEGORIES:
            category = "backend_error"

        finding = Finding(
            agent="TriageAgent",
            summary=triage_output.reasoning or f"Bug classified as: {category}",
            confidence=triage_output.confidence,
        )
    except Exception:
        # Fallback: unstructured call if structured output fails
        response = await llm.ainvoke(prompt)
        content = str(response.content).strip().lower()

        # Try to find a valid category in the response
        category = "backend_error"
        for valid_cat in VALID_CATEGORIES:
            if valid_cat in content:
                category = valid_cat
                break

        finding = Finding(
            agent="TriageAgent",
            summary=f"Bug classified as: {category}",
            confidence=0.5,
        )

    return {
        "bug_category": category,
        "findings": [finding],
    }
