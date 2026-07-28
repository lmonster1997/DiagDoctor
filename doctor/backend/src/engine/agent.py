"""
DiagnosisAgent — V3 统一诊断 Agent (ReAct with full toolset).

Uses LangChain's ``create_agent`` to build a ReAct agent that:
1. Receives normalized evidence via HumanMessage
2. Calls 5 tools on demand
3. Produces a structured DiagnosisReport

Agent is cached at module level for reuse across sessions.
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
    return get_llm_for_role("diagnosis")


def _get_tools() -> list[BaseTool]:
    return list(get_all_tools())


def load_tools_reference() -> str:
    """Load the tools reference markdown document for the System Prompt."""
    # engine/agent.py → parent=engine/ → parent.parent=src/
    ref_path = (
        Path(__file__).resolve().parent.parent / "prompts" / "templates" / "tools_reference.md"
    )
    if ref_path.exists():
        return ref_path.read_text(encoding="utf-8")
    logger.warning("tools_reference_md_not_found", path=str(ref_path))
    return "(工具文档未找到，请根据工具名称和描述自行判断用法)"


def _build_system_prompt() -> str:
    tools_ref = load_tools_reference()
    return render_prompt("diagnosis_agent.j2", tools_reference=tools_ref)


def build_diagnosis_agent() -> Any:
    """Build the DiagnosisAgent ReAct agent with 7 middlewares.

    Middleware registration order (verified via verify_middleware_assumptions.py):
    - abefore_agent: runs in registration order
    - wrap_tool_call: outer→inner (first registered wraps outermost)
    - after_agent: reverse registration order

    Pipeline: AgentLifecycle → ToolDedup → LangfuseTracing
              → ToolTruncation → ContextElision → BudgetGuard → ForcedFinalCall
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

    from src.engine.budget.guard import BudgetGuardMiddleware
    from src.engine.middleware.context_elision import ContextElisionMiddleware
    from src.engine.middleware.forced_call import ForcedFinalCallMiddleware
    from src.engine.middleware.langfuse_tracing import LangfuseTracingMiddleware
    from src.engine.middleware.lifecycle import AgentLifecycleMiddleware
    from src.engine.middleware.tool_dedup import ToolDedupMiddleware
    from src.engine.middleware.tool_truncation import ToolTruncationMiddleware

    middleware = [
        AgentLifecycleMiddleware(),
        ToolDedupMiddleware(),
        LangfuseTracingMiddleware(),
        ToolTruncationMiddleware(),
        ContextElisionMiddleware(),
        BudgetGuardMiddleware(),
        ForcedFinalCallMiddleware(),
    ]

    agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
        middleware=middleware,
    )

    return agent


def get_diagnosis_agent() -> CompiledStateGraph:  # type: ignore[type-arg]
    """Get or create the cached DiagnosisAgent instance."""
    global _diagnosis_agent_cache
    if _diagnosis_agent_cache is None:
        _diagnosis_agent_cache = build_diagnosis_agent()
    return _diagnosis_agent_cache


def clear_diagnosis_agent_cache() -> None:
    """Clear the cached agent (useful for testing or hot-reload)."""
    global _diagnosis_agent_cache
    _diagnosis_agent_cache = None
    logger.info("diagnosis_agent_cache_cleared")
