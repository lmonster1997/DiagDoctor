"""The ReAct loop body — extracted from ``diagnosis_agent_node``.

This module owns the per-iteration mechanics:
- Hard-constraint check (token / time budget)
- LLM ainvoke (with tools bound)
- Natural-stop detection (no tool_calls → break)
- Tool-call deduplication (skip identical repeats)
- Tool execution (errors caught, not propagated)
- Static tool-result truncation (prevent single result from blowing context)
- Langfuse span recording for each tool call
- for-else cap handling (loop ran to MAX_TOOL_CALLS → budget_exhausted=True)

Pure extraction — no behavior change vs the original inline loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import BaseTool

from src.graph.context_engine import ContextBudget, truncate_tool_result
from src.graph.nodes.diagnosis_agent.constants import (
    MAX_TIME_SECONDS,
    MAX_TOKENS_BUDGET,
    MAX_TOOL_CALLS,
)
from src.observability.logger import get_logger

logger = get_logger(__name__)


async def _run_react_loop(
    messages: list[BaseMessage],
    llm_with_tools: BaseChatModel,
    tool_map: dict[str, BaseTool],
    ctx_budget: ContextBudget,
    langfuse_handler: Any | None,
    invoke_config: dict[str, Any],
    case_id: str,
) -> bool:
    """Run the ReAct loop. Returns ``budget_exhausted`` (True if hit cap or hard limit).

    Mutates ``messages`` in place: appends each AIMessage and ToolMessage as
    the loop progresses so the caller can parse the final state.

    Args:
        messages: Conversation history — mutated in place (AI + Tool messages
            appended each iteration).
        llm_with_tools: Tool-bound diagnosis LLM driving the loop.
        tool_map: {tool_name: BaseTool} for execution.
        ctx_budget: ContextBudget for token/iteration accounting.
        langfuse_handler: Optional Langfuse handler for span recording.
        invoke_config: Langfuse callback config (passed to ainvoke).
        case_id: For logging.

    Returns:
        True if the loop hit MAX_TOOL_CALLS cap or a hard token/time limit;
        False if the agent natural-stopped (no tool_calls).
    """
    # Tool-call dedup cache (efficiency optimization, doesn't change agent decisions)
    call_history: list[tuple[str, str]] = []
    budget_exhausted = False

    for iteration in range(MAX_TOOL_CALLS):
        ctx_budget.tick_iteration()

        # ── Hard-constraint check (token / time) ──────────────────────
        if (
            ctx_budget.total_used >= MAX_TOKENS_BUDGET
            or ctx_budget.elapsed_seconds >= MAX_TIME_SECONDS
        ):
            logger.warning(
                "budget_hard_limit_hit",
                iteration=iteration + 1,
                total_used=ctx_budget.total_used,
                elapsed_seconds=round(ctx_budget.elapsed_seconds, 1),
                case_id=case_id,
            )
            budget_exhausted = True
            break

        response: AIMessage = await asyncio.wait_for(
            llm_with_tools.ainvoke(
                messages,
                config=invoke_config if invoke_config else None,  # type: ignore[arg-type]
            ),
            timeout=MAX_TIME_SECONDS,
        )
        messages.append(response)
        ctx_budget.add_agent_reasoning(str(response.content))

        # No tool_calls → agent considers diagnosis done, natural stop
        if not response.tool_calls:
            logger.info(
                "agent_natural_stop",
                iteration=iteration + 1,
                case_id=case_id,
            )
            break

        # Process this iteration's tool_calls
        for tc in response.tool_calls:
            tool_name = tc["name"]
            tool_args = tc["args"]

            # ── Tool-call dedup ──────────────────────────────────────
            call_key = (tool_name, json.dumps(tool_args, sort_keys=True))
            if call_key in call_history:
                logger.debug(
                    "tool_call_skipped_duplicate",
                    tool_name=tool_name,
                    iteration=iteration + 1,
                )
                if langfuse_handler is not None:
                    with contextlib.suppress(Exception):
                        langfuse_handler.record_tool_skipped(
                            tool_name=tool_name,
                            tool_args=tool_args,
                            iteration=iteration + 1,
                        )
                messages.append(
                    ToolMessage(
                        content="[跳过：与之前调用完全相同]",
                        tool_call_id=tc["id"],
                        name=tool_name,
                    )
                )
                continue
            call_history.append(call_key)

            # ── Execute tool (errors don't break the loop) ────────────
            tool_t0 = time.monotonic()
            tool_error: str | None = None
            try:
                result = await tool_map[tool_name].ainvoke(tool_args)
            except Exception as tool_exc:
                logger.warning(
                    "tool_execution_error",
                    tool_name=tool_name,
                    error=str(tool_exc),
                    iteration=iteration + 1,
                )
                tool_error = str(tool_exc)
                result = f"工具执行错误: {tool_exc}"
            tool_latency_ms = (time.monotonic() - tool_t0) * 1000

            # ── Static tool-result truncation (prevent context blowup) ──
            result_str = truncate_tool_result(tool_name, str(result))
            ctx_budget.add_tool_call(1)

            # ── Record tool call as Langfuse SPAN ─────────────────────
            if langfuse_handler is not None:
                with contextlib.suppress(Exception):
                    langfuse_handler.record_tool_span(
                        tool_name=tool_name,
                        tool_args=tool_args,
                        result=result_str,
                        latency_ms=tool_latency_ms,
                        iteration=iteration + 1,
                        error=tool_error,
                    )

            ctx_budget.add_tool_result(result_str)

            messages.append(
                ToolMessage(
                    content=result_str,
                    tool_call_id=tc["id"],
                    name=tool_name,
                )
            )

            logger.debug(
                "tool_executed",
                tool_name=tool_name,
                iteration=iteration + 1,
                result_len=len(result_str),
                latency_ms=round(tool_latency_ms, 1),
                budget_tool_tokens=ctx_budget.tool_result_tokens,
                budget_agent_tokens=ctx_budget.agent_reasoning_tokens,
            )
    else:
        # for-else: loop ran MAX_TOOL_CALLS iterations without break
        logger.warning(
            "max_iterations_reached",
            max_iterations=MAX_TOOL_CALLS,
            case_id=case_id,
            tool_calls=ctx_budget.tool_calls,
            elapsed_seconds=round(ctx_budget.elapsed_seconds, 1),
        )
        budget_exhausted = True

    return budget_exhausted
