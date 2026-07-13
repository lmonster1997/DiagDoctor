"""AgentLifecycleMiddleware — per-invocation state initialisation.

Registered FIRST in the middleware list so its ``abefore_agent`` runs
before any other middleware sees the run context.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware

from src.engine.context.budget import ContextBudget
from src.engine.run_context import get_run_context_or_none
from src.observability.logger import get_logger

logger = get_logger(__name__)


class AgentLifecycleMiddleware(AgentMiddleware):
    """Initialise per-invocation budget, counters, and dedup history."""

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        ctx = get_run_context_or_none()
        if ctx is None:
            return None

        ctx.ctx_budget = ContextBudget()
        if ctx.system_prompt_text:
            ctx.ctx_budget.add_system_prompt(ctx.system_prompt_text)
        if ctx.evidence_text:
            ctx.ctx_budget.add_evidence(ctx.evidence_text)
        ctx.ctx_budget.start_timer()

        ctx.call_history = []
        ctx.model_call_count = 0
        ctx.budget_exhausted = False
        ctx.forced_call_triggered = False

        logger.debug(
            "agent_lifecycle_initialised",
            case_id=ctx.case_id,
            budget_tokens=ctx.ctx_budget.total_used,
        )
        return None
