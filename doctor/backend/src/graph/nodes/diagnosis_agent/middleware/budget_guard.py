"""BudgetGuardMiddleware — MAX_TOOL_CALLS / token / time caps → jump_to end.

Maps to the hand-written loop's hard-constraint block:
- ``abefore_model`` ← ``react_loop.py:71-87`` (tick_iteration + token/time cap check → break)
  Plus the ``for-else`` cap detection: ``range(MAX_TOOL_CALLS)`` exhausting sets
  ``budget_exhausted=True``. In middleware, ``model_call_count`` tracks LLM
  calls and tripping ``> MAX_TOOL_CALLS`` jumps to end with the flag set.
- ``aafter_model`` ← ``react_loop.py:97`` (add_agent_reasoning token accounting)

Uses ``@hook_config(can_jump_to=["end"])`` so the agent graph wires a
conditional edge that reads ``state["jump_to"]``. Returning
``{"jump_to": "end"}`` stops the loop; ForcedFinalCallMiddleware then runs in
``aafter_agent`` to attempt JSON delivery (same separation as the hand-written
loop: cap break first, forced call after).

``recursion_limit=30`` on ``agent.ainvoke`` is a hard backstop in case both
this middleware and LangGraph's own counter fail — the per-LLM-call count here
is the primary cap (matches ``range(12)`` semantics exactly: 12 LLM calls
allowed, the 13th triggers jump before it executes).
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, BaseMessage

from src.graph.nodes.diagnosis_agent.budget import (
    MAX_TIME_SECONDS,
    MAX_TOKENS_BUDGET,
    MAX_TOOL_CALLS,
)
from src.graph.nodes.diagnosis_agent.middleware.run_context import (
    get_run_context,
    get_run_context_or_none,
)
from src.observability.logger import get_logger

logger = get_logger(__name__)


class BudgetGuardMiddleware(AgentMiddleware):
    """Enforce MAX_TOOL_CALLS / token / time hard caps via jump_to='end'."""

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        ctx = get_run_context()
        ctx.model_call_count += 1
        ctx.ctx_budget.tick_iteration()

        # ── Iteration cap (matches range(MAX_TOOL_CALLS) — 12 LLM calls allowed) ──
        if ctx.model_call_count > MAX_TOOL_CALLS:
            ctx.budget_exhausted = True
            logger.warning(
                "budget_iteration_cap_hit",
                case_id=ctx.case_id,
                model_call_count=ctx.model_call_count,
                max=MAX_TOOL_CALLS,
            )
            return {"jump_to": "end"}

        # ── Token / time hard cap (matches react_loop.py:75-87) ──
        if (
            ctx.ctx_budget.total_used >= MAX_TOKENS_BUDGET
            or ctx.ctx_budget.elapsed_seconds >= MAX_TIME_SECONDS
        ):
            ctx.budget_exhausted = True
            logger.warning(
                "budget_hard_limit_hit",
                case_id=ctx.case_id,
                model_call_count=ctx.model_call_count,
                total_used=ctx.ctx_budget.total_used,
                elapsed_seconds=round(ctx.ctx_budget.elapsed_seconds, 1),
            )
            return {"jump_to": "end"}

        return None

    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        ctx = get_run_context_or_none()
        if ctx is None:
            return None
        # add_agent_reasoning on the just-produced AIMessage (matches react_loop.py:97)
        messages: list[BaseMessage] = state.get("messages", []) if isinstance(state, dict) else []
        for msg in reversed(messages):
            if isinstance(msg, AIMessage):
                ctx.ctx_budget.add_agent_reasoning(str(msg.content))
                break
        return None
