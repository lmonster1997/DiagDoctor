"""BudgetGuardMiddleware — MAX_TOOL_CALLS / token / time caps → jump_to end.

Registered 5th in the middleware pipeline (before ForcedFinalCall).
Uses ``@hook_config(can_jump_to=["end"])`` to stop the agent loop
when any budget dimension is exhausted.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, BaseMessage

from src.engine.budget.constants import MAX_TIME_SECONDS, MAX_TOKENS_BUDGET, MAX_TOOL_CALLS
from src.engine.run_context import get_run_context, get_run_context_or_none
from src.observability.logger import get_logger

logger = get_logger(__name__)


class BudgetGuardMiddleware(AgentMiddleware):
    """Enforce MAX_TOOL_CALLS / token / time hard caps via jump_to='end'."""

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        ctx = get_run_context()
        ctx.model_call_count += 1
        ctx.ctx_budget.tick_iteration()

        if ctx.model_call_count > MAX_TOOL_CALLS:
            ctx.budget_exhausted = True
            logger.warning(
                "budget_iteration_cap_hit",
                case_id=ctx.case_id,
                model_call_count=ctx.model_call_count,
                max=MAX_TOOL_CALLS,
            )
            return {"jump_to": "end"}

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
        messages: list[BaseMessage] = state.get("messages", []) if isinstance(state, dict) else []
        for msg in reversed(messages):
            if isinstance(msg, AIMessage):
                ctx.ctx_budget.add_agent_reasoning(str(msg.content))
                break
        return None

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        ctx = get_run_context_or_none()
        result = await handler(request)
        if ctx is not None:
            try:
                ctx.ctx_budget.add_tool_call(1)
                content = str(getattr(result, "content", result))
                ctx.ctx_budget.add_tool_result(content)
            except Exception:
                pass
        return result
