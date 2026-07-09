"""
DiagnosisAgent subgraph — V3 统一诊断 Agent (ReAct with full toolset).

Replaces V2's multi-specialist fan-out architecture with a single agent
that has access to ALL 5 tools and can diagnose any Web app bug type.

Uses LangChain's ``create_agent`` to build a ReAct agent that:
1. Receives normalized evidence (golden_signals + correlations) via HumanMessage
2. Calls tools (search_observability, code_search, db_query, inspect_frontend_error,
   get_file_content) on demand
3. Produces a structured DiagnosisReport with root cause and fix suggestion

Design:
    - System prompt from ``templates/diagnosis_agent.j2`` (Jinja2, cached)
    - Tools from ``src.tools.ALL_TOOLS`` (V3 unified 5-tool set)
    - LLM: ``get_llm_for_role("diagnosis")`` (strongest model, same tier as specialist)
    - Agent cached at module level for reuse across diagnosis sessions

Usage::

    from src.graph.subgraphs.diagnosis_agent import get_diagnosis_agent

    agent = get_diagnosis_agent()
    result = await agent.ainvoke({"messages": [HumanMessage(content=evidence_text)]})
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from src.config import settings
from src.llm_factory import get_llm_for_role
from src.observability.logger import get_logger
from src.prompts.registry import render_prompt
from src.tools import get_all_tools

logger = get_logger(__name__)

# ── Module-level cache ───────────────────────────────────────────────

_diagnosis_agent_cache: CompiledStateGraph | None = None  # type: ignore[type-arg]


def _get_llm() -> BaseChatModel:
    """Get the LLM instance for the DiagnosisAgent (diagnosis tier = strongest)."""
    return get_llm_for_role("diagnosis")


def _get_tools() -> list[BaseTool]:
    """Get the V3 unified tool set (5 tools)."""
    return list(get_all_tools())


def load_tools_reference() -> str:
    """
    Load the tools reference markdown document for the System Prompt.

    Reads ``tools_reference.md`` from the prompts templates directory.
    This document is injected into the agent's system prompt so it knows
    what tools are available and how to use them.

    Returns:
        Full content of tools_reference.md as a string.
    """
    ref_path = (
        Path(__file__).resolve().parent.parent.parent
        / "prompts"
        / "templates"
        / "tools_reference.md"
    )
    if ref_path.exists():
        return ref_path.read_text(encoding="utf-8")
    logger.warning("tools_reference_md_not_found", path=str(ref_path))
    return "(工具文档未找到，请根据工具名称和描述自行判断用法)"


def _build_system_prompt() -> str:
    """
    Render the DiagnosisAgent system prompt from the Jinja2 template.

    Only ``tools_reference`` is injected — evidence data is passed via
    the user message at runtime (not baked into the system prompt).
    """
    tools_ref = load_tools_reference()
    return render_prompt("diagnosis_agent.j2", tools_reference=tools_ref)


def build_diagnosis_agent() -> Any:  # CompiledStateGraph (relaxed per B2 policy)
    """
    Build the DiagnosisAgent ReAct agent.

    Uses LangChain's ``create_agent`` with:
    - All 5 V3 tools (search_observability, code_search, db_query,
      inspect_frontend_error, get_file_content)
    - System prompt from diagnosis_agent.j2 template
    - LLM configured via ``get_llm_for_role("diagnosis")``
    - 5 harness middlewares (replaces the hand-written ReAct loop in
      ``nodes/diagnosis_agent/react_loop.py``):

      Registration order matters — verified via
      ``scripts/verify_middleware_assumptions.py``:
      - ``wrap_tool_call`` runs outer→inner (first registered wraps outermost)
        → [ToolDedup, LangfuseTracing, ToolTruncation] gives dedup short-circuit
        outermost, Langfuse span recording middle (sees truncated result),
        truncation innermost.
      - ``after_agent`` runs in reverse registration order → ForcedFinalCall
        (registered last) runs first, then LangfuseTracing's end_trace — so
        end_trace captures ``forced_call_triggered=True``.

    Returns:
        A compiled LangGraph state graph (ReAct agent with middleware).
    """
    llm = _get_llm()
    tools = _get_tools()
    system_prompt = _build_system_prompt()

    logger.info(
        "building_diagnosis_agent",
        model=settings.llm_specialist_model or settings.llm_model,
        tool_count=len(tools),
        tool_names=[t.name for t in tools],
    )

    # Lazy import to avoid a circular import at module load time:
    # src.graph.nodes.diagnosis_agent.middleware lives under the
    # src.graph.nodes package, whose __init__ eagerly imports
    # diagnosis_agent_node, which imports _build_system_prompt from this
    # module. Importing middleware at top of this module would re-enter this
    # module before _build_system_prompt is defined. Deferring to build time
    # (called from get_diagnosis_agent / tests) breaks the cycle cleanly.
    from src.graph.nodes.diagnosis_agent.middleware import (
        BudgetGuardMiddleware,
        ForcedFinalCallMiddleware,
        LangfuseTracingMiddleware,
        ToolDedupMiddleware,
        ToolTruncationMiddleware,
    )

    middleware = [
        ToolDedupMiddleware(),
        LangfuseTracingMiddleware(),
        ToolTruncationMiddleware(),
        BudgetGuardMiddleware(),
        ForcedFinalCallMiddleware(),
    ]

    # Phase 0: append CopilotKitMiddleware so the agent can stream
    # chat, tool calls, and HITL interrupts to the CopilotKit frontend.
    # CopilotKit's middleware forwards AG-UI protocol events; it is
    # intentionally placed last (innermost) to not interfere with the
    # existing 5 harness middlewares.
    try:
        from copilotkit import CopilotKitMiddleware  # type: ignore[import-untyped]

        middleware.append(CopilotKitMiddleware())
        logger.info("copilotkit_middleware_added")
    except ImportError:
        logger.warning("copilotkit_not_installed_skipping_middleware")

    agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
        middleware=middleware,
    )

    return agent


def get_diagnosis_agent() -> CompiledStateGraph:  # type: ignore[type-arg]
    """
    Get or create the cached DiagnosisAgent instance.

    The agent is built once and reused across all diagnosis sessions
    to avoid re-creating the LLM, tools, and system prompt for each request.
    """
    global _diagnosis_agent_cache
    if _diagnosis_agent_cache is None:
        _diagnosis_agent_cache = build_diagnosis_agent()
    return _diagnosis_agent_cache


def clear_diagnosis_agent_cache() -> None:
    """Clear the cached agent (useful for testing or hot-reload)."""
    global _diagnosis_agent_cache
    _diagnosis_agent_cache = None
    logger.info("diagnosis_agent_cache_cleared")
