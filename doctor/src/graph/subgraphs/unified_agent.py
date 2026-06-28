"""
UnifiedAgent subgraph — V3 统一诊断 Agent (ReAct with full toolset).

Replaces V2's multi-specialist fan-out architecture with a single agent
that has access to ALL 5 tools and can diagnose any Web app bug type.

Uses LangChain's ``create_agent`` to build a ReAct agent that:
1. Receives normalized evidence (golden_signals + correlations) via HumanMessage
2. Calls tools (search_observability, code_search, db_query, inspect_frontend_error,
   get_file_content) on demand
3. Produces a structured DiagnosisReport with root cause and fix suggestion

Design:
    - System prompt from ``templates/unified_agent.j2`` (Jinja2, cached)
    - Tools from ``src.tools.ALL_TOOLS`` (V3 unified 5-tool set)
    - LLM: ``get_llm_for_role("diagnosis")`` (strongest model, same tier as specialist)
    - Agent cached at module level for reuse across diagnosis sessions

Usage::

    from src.graph.subgraphs.unified_agent import get_unified_agent

    agent = get_unified_agent()
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

_unified_agent_cache: CompiledStateGraph | None = None  # type: ignore[type-arg]


def _get_llm() -> BaseChatModel:
    """Get the LLM instance for the UnifiedAgent (diagnosis tier = strongest)."""
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
    Render the UnifiedAgent system prompt from the Jinja2 template.

    Only ``tools_reference`` is injected — evidence data is passed via
    the user message at runtime (not baked into the system prompt).
    """
    tools_ref = load_tools_reference()
    return render_prompt("unified_agent.j2", tools_reference=tools_ref)


def build_unified_agent() -> Any:  # CompiledStateGraph (relaxed per B2 policy)
    """
    Build the UnifiedAgent ReAct agent.

    Uses LangChain's ``create_agent`` with:
    - All 5 V3 tools (search_observability, code_search, db_query,
      inspect_frontend_error, get_file_content)
    - System prompt from unified_agent.j2 template
    - LLM configured via ``get_llm_for_role("diagnosis")``

    Returns:
        A compiled LangGraph state graph (ReAct agent).
    """
    llm = _get_llm()
    tools = _get_tools()
    system_prompt = _build_system_prompt()

    logger.info(
        "building_unified_agent",
        model=settings.llm_specialist_model or settings.llm_model,
        tool_count=len(tools),
        tool_names=[t.name for t in tools],
    )

    agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
    )

    return agent


def get_unified_agent() -> CompiledStateGraph:  # type: ignore[type-arg]
    """
    Get or create the cached UnifiedAgent instance.

    The agent is built once and reused across all diagnosis sessions
    to avoid re-creating the LLM, tools, and system prompt for each request.
    """
    global _unified_agent_cache
    if _unified_agent_cache is None:
        _unified_agent_cache = build_unified_agent()
    return _unified_agent_cache


def clear_unified_agent_cache() -> None:
    """Clear the cached agent (useful for testing or hot-reload)."""
    global _unified_agent_cache
    _unified_agent_cache = None
    logger.info("unified_agent_cache_cleared")
