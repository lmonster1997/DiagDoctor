"""
Backend Specialist subgraph — ReAct Agent for backend bug diagnosis.

Uses LangChain's ``create_agent`` to build an agent that:
1. Analyses normalized evidence (golden_signals + correlations)
2. Calls shared tools (log_search, trace_query, code_search, db_query) on demand
3. Produces a structured Finding with code-level root cause and fix suggestions

The agent is instantiated once and cached for reuse across diagnosis sessions.

Design:
    - System prompt from ``templates/backend_specialist.j2`` (Jinja2)
    - Tools from ``src.tools`` shared pool (LOKI_QUERY, TEMPO, CODE_SEARCH, DB_QUERY)
    - LLM from ``src.config.settings`` (supports any OpenAI-compatible API)
    - Finding parsed from the agent's final message

Usage::

    from src.graph.subgraphs.backend_specialist import get_backend_specialist

    agent = get_backend_specialist()
    result = await agent.ainvoke({"messages": [HumanMessage(content=...)]})
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from src.config import settings
from src.graph.state import Finding
from src.llm_factory import get_llm_for_role
from src.observability.logger import get_logger
from src.prompts.registry import render_prompt
from src.tools import SHARED_TOOLS

logger = get_logger(__name__)

# ── Module-level cache ───────────────────────────────────────────────

_backend_specialist_agent: CompiledStateGraph | None = None  # type: ignore[type-arg]


def _get_llm() -> BaseChatModel:
    """Get the LLM instance for the backend specialist (specialist-tier)."""
    return get_llm_for_role("specialist")


def get_backend_specialist_tools() -> list[BaseTool]:
    """Get the shared tool set for the backend specialist agent."""
    return list(SHARED_TOOLS)


def _build_system_prompt() -> str:
    """Render the Backend Specialist system prompt from the Jinja2 template.

    The template provides core instructions and tool descriptions.
    Evidence data (golden_signals, correlations) is NOT in the system
    prompt — it is passed via the user message at runtime.
    """
    return render_prompt("backend_specialist.j2")


def build_backend_specialist() -> Any:  # CompiledStateGraph (relaxed per B2 policy)
    """
    Build the Backend Specialist ReAct agent.

    Uses LangChain's ``create_agent`` with:
    - Shared tools from the DiagDoctor tool pool
    - System prompt from the backend_specialist.j2 template
    - LLM configured from settings

    Returns:
        A compiled LangGraph state graph (ReAct agent).
    """
    llm = _get_llm()
    tools = get_backend_specialist_tools()
    system_prompt = _build_system_prompt()

    logger.info(
        "building_backend_specialist",
        model=settings.llm_model,
        tool_count=len(tools),
    )

    agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
    )

    return agent


def get_backend_specialist() -> CompiledStateGraph:  # type: ignore[type-arg]
    """
    Get or create the cached Backend Specialist agent.

    The agent is built once and reused across all diagnosis sessions
    to avoid re-creating the LLM and tools for each request.
    """
    global _backend_specialist_agent
    if _backend_specialist_agent is None:
        _backend_specialist_agent = build_backend_specialist()
    return _backend_specialist_agent


# ── Output parsing ───────────────────────────────────────────────────


def parse_agent_output_to_finding(agent_result: dict[str, Any]) -> Finding:
    """
    Parse the ReAct agent's final output into a structured Finding.

    The agent's final message should contain a JSON block with the Finding
    fields. If JSON parsing fails, extracts what we can from the raw text.

    Expected JSON format from the agent::

        {
            "summary": "根因一句话描述",
            "affected_files": ["path/to/file.py"],
            "fix_suggestion": "具体修复建议...",
            "evidence_refs": ["sig-xxx", "span-yyy"],
            "confidence": 0.85,
            "contradiction": false,
            "cross_layer": false
        }

    Args:
        agent_result: The full state dict returned by ``agent.ainvoke()``.

    Returns:
        A Finding model instance.
    """
    messages: list[Any] = agent_result.get("messages", [])

    # Find the last AI message
    last_ai_content = ""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            last_ai_content = str(msg.content)
            break

    if not last_ai_content:
        logger.warning("no_ai_message_in_agent_result")
        return Finding(
            agent="backend_specialist",
            summary="（Agent 未返回有效诊断）",
            confidence=0.0,
        )

    # Try to extract JSON block from the response
    finding_data = _extract_json_from_text(last_ai_content)

    if finding_data:
        try:
            return Finding(
                agent="backend_specialist",
                summary=str(finding_data.get("summary", "")),
                evidence_refs=_ensure_list(finding_data.get("evidence_refs", [])),
                affected_files=_ensure_list(finding_data.get("affected_files", [])),
                fix_suggestion=str(finding_data.get("fix_suggestion", "")),
                confidence=float(finding_data.get("confidence", 0.5)),
                cross_layer=bool(finding_data.get("cross_layer", False)),
                contradiction=bool(finding_data.get("contradiction", False)),
            )
        except (ValueError, TypeError) as exc:
            logger.warning(
                "failed_to_parse_finding_json",
                error=str(exc),
                content_preview=last_ai_content[:500],
            )

    # Fallback: extract what we can from raw text
    return Finding(
        agent="backend_specialist",
        summary=last_ai_content[:500] if last_ai_content else "（无法解析 Agent 输出）",
        evidence_refs=[],
        affected_files=[],
        fix_suggestion="",
        confidence=0.3,
    )


def _extract_json_from_text(text: str) -> dict[str, Any] | None:
    """Try to extract a JSON object from text (handles markdown code fences)."""
    # Try to find JSON in markdown code fences
    json_pattern = r"```(?:json)?\s*\n?(.*?)\n?```"
    matches = re.findall(json_pattern, text, re.DOTALL)
    for match in matches:
        try:
            return json.loads(match)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            continue

    # Try to find raw JSON object in text
    brace_pattern = r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}"
    matches = re.findall(brace_pattern, text, re.DOTALL)
    for match in matches:
        try:
            return json.loads(match)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            continue

    return None


def _ensure_list(value: Any) -> list[str]:
    """Ensure a value is a list of strings."""
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value] if value else []
    return []


# ── Cache reset (for testing) ────────────────────────────────────────


def reset_backend_specialist() -> None:
    """Reset the cached agent and LLM (useful for testing)."""
    global _backend_specialist_agent
    _backend_specialist_agent = None
    from src.llm_factory import clear_llm_cache

    clear_llm_cache()
    logger.debug("backend_specialist_cache_reset")
