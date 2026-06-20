"""
Main LangGraph definition for DiagDoctor diagnosis pipeline.

Implements a minimal runnable graph with:
- dummy_triage_node: classifies bug category via LLM (structured output)
- dummy_reporter_node: generates a diagnosis report

Graph flow: START → triage → reporter → END

Uses MemorySaver for checkpoint persistence, enabling:
- Resumable diagnosis sessions via thread_id
- State inspection and replay

Note: For production, switch to AsyncSqliteSaver from
langgraph.checkpoint.sqlite.aio for persistent storage.
"""

from __future__ import annotations

import uuid
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from src.config import settings
from src.graph.state import DiagnosisReport, DoctorState, Finding

# ── Structured output models ────────────────────────────────────────


class TriageOutput(BaseModel):
    """Structured output from the triage LLM call."""

    category: str = Field(description="Bug category classification")
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    reasoning: str = Field(default="", description="Brief reasoning for the classification")


# ── Graph nodes ─────────────────────────────────────────────────────


async def dummy_triage_node(state: DoctorState) -> dict[str, Any]:
    """
    Triage node: calls LLM with structured output to classify bug category.

    Uses LangChain's with_structured_output() to guarantee parseable JSON.
    Returns updated bug_category, a Finding, and messages.
    """
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        temperature=0.1,
    )

    user_report = state.evidence.user_report

    prompt = f"""你是一个 Bug 分类专家。基于以下用户报告，判断 bug 的类别。

【用户报告】
{user_report}

【可能的类别】
- frontend_crash: 前端运行时崩溃（白屏、JS 错误）
- backend_error: 后端异常（5xx、未处理异常）
- performance: 性能问题（慢、超时）
- logic: 业务逻辑错误（数据不对、流程错乱）
- data: 数据问题（编码、精度、时区）
- config: 配置或环境问题"""

    valid_categories = {
        "frontend_crash", "backend_error", "performance", "logic", "data", "config",
    }

    try:
        structured_llm = llm.with_structured_output(TriageOutput)
        raw_output = await structured_llm.ainvoke(prompt)
        triage_output = TriageOutput.model_validate(raw_output) if isinstance(raw_output, dict) else raw_output

        category = triage_output.category.strip().lower()
        if category not in valid_categories:
            category = "backend_error"

        finding = Finding(
            agent="TriageAgent",
            summary=triage_output.reasoning or f"Bug classified as: {category}",
            confidence=triage_output.confidence,
        )
    except Exception:
        # Fallback: unstructured call if structured output fails
        response = await llm.ainvoke(prompt)
        category = str(response.content).strip().lower()
        if category not in valid_categories:
            category = "backend_error"

        finding = Finding(
            agent="TriageAgent",
            summary=f"Bug classified as: {category}",
            confidence=0.5,
        )

    return {
        "bug_category": category,
        "findings": [finding],
    }


async def dummy_reporter_node(state: DoctorState) -> dict[str, Any]:
    """
    Reporter node: generates a diagnosis report based on triage result.

    In future iterations this will synthesize all findings and hypotheses
    into a comprehensive report.
    """
    bug_category = state.bug_category or "unknown"

    category_descriptions: dict[str, str] = {
        "frontend_crash": "前端运行时崩溃，可能与 JS 错误、组件渲染异常或未捕获的 Promise 相关。",
        "backend_error": "后端服务异常，可能与未处理异常、数据库错误或外部服务调用失败相关。",
        "performance": "性能问题，可能与 N+1 查询、缓存缺失或慢 API 调用相关。",
        "logic": "业务逻辑错误，可能与权限校验、数据状态管理或流程控制相关。",
        "data": "数据问题，可能与编码、精度丢失或时区处理相关。",
        "config": "配置或环境问题，可能与 CORS、环境变量或服务发现相关。",
    }

    description = category_descriptions.get(bug_category, "需要进一步排查。")

    report = DiagnosisReport(
        bug_category=bug_category,
        root_cause=f"初步分析：此问题属于 {bug_category} 类型。{description}",
        fix_suggestion="建议检查相关日志和 Trace 以定位具体根因。",
        evidence_chain=["TriageAgent 分类"],
        confidence=0.5,
    )

    return {"report": report}


# ── Graph construction ──────────────────────────────────────────────

_graph_instance: Any = None


def _get_checkpointer() -> MemorySaver:
    """Create a MemorySaver checkpointer for development.

    Note: For production, replace with AsyncSqliteSaver which requires
    async context manager lifecycle management at the application level.
    """
    return MemorySaver()


def build_graph() -> Any:
    """Build (but do not compile) the DiagDoctor diagnosis graph."""
    graph: StateGraph[DoctorState, None, DoctorState, DoctorState] = StateGraph(DoctorState)

    graph.add_node("triage", dummy_triage_node)
    graph.add_node("reporter", dummy_reporter_node)

    graph.set_entry_point("triage")
    graph.add_edge("triage", "reporter")
    graph.add_edge("reporter", END)

    return graph


def get_graph() -> Any:
    """
    Get or create the compiled DiagDoctor graph with SqliteSaver checkpointer.

    The graph is cached at module level for reuse across requests.
    Uses SqliteSaver so diagnosis sessions can be resumed via thread_id.
    """
    global _graph_instance
    if _graph_instance is None:
        checkpointer = _get_checkpointer()
        _graph_instance = build_graph().compile(checkpointer=checkpointer)
    return _graph_instance


def generate_thread_id() -> str:
    """Generate a unique thread_id for a new diagnosis session."""
    return f"diag-{uuid.uuid4().hex[:12]}"
