"""ForcedFinalCallMiddleware — post-loop forced JSON call (structured output).

After the create_agent loop ends, forces one final LLM call with
``with_structured_output`` to ensure a valid JSON diagnosis report.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel

import src.llm_factory as _llm_factory
from src.engine.forced_call import (
    _forced_final_json_call,
    _last_ai_has_json,
    _last_ai_is_natural_stop,
)
from src.engine.run_context import get_run_context
from src.observability.logger import get_logger

logger = get_logger(__name__)


class ForcedFinalCallMiddleware(AgentMiddleware):
    """Force one final structured-output LLM call if the loop didn't deliver JSON."""

    async def aafter_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        ctx = get_run_context()
        messages = state.get("messages", []) if isinstance(state, dict) else []

        if not messages:
            return None

        # Healthy case: the agent already emitted a JSON report on its final
        # AIMessage -> skip the extra structured-output call. Saves one
        # full-history LLM call per healthy run with no regression (the
        # ``_last_ai_has_json`` guard already exists and is unit-tested in
        # test_forced_final_json_call.py::TestLastAiHasJson).
        # NOTE: budget exhaustion is intentionally NOT a skip condition -
        # the forced call exists to recover the mode-1 failure (loop hit
        # MAX_MODEL_CALLS with content="" + tool_calls), so it must still fire.
        if _last_ai_has_json(messages):
            logger.info("forced_call_skipped_last_ai_has_json", case_id=ctx.case_id)
            return None

        natural_stop = _last_ai_is_natural_stop(messages)
        ctx.forced_call_triggered = True
        logger.info(
            "forced_final_json_call_triggered",
            case_id=ctx.case_id,
            natural_stop=natural_stop,
            budget_exhausted=ctx.budget_exhausted,
        )

        fresh_llm: BaseChatModel = _llm_factory.get_llm_for_role("diagnosis")

        invoke_config: dict[str, Any] = {}
        if ctx.langfuse_handler is not None:
            invoke_config["callbacks"] = [ctx.langfuse_handler]

        forced_response = await _forced_final_json_call(
            messages=messages,
            llm=fresh_llm,
            invoke_config=invoke_config,
            natural_stop=natural_stop,
            case_id=ctx.case_id,
            langfuse_handler=ctx.langfuse_handler,
        )

        if forced_response is not None:
            ctx.ctx_budget.add_agent_reasoning(str(forced_response.content))
            return {"messages": [forced_response]}

        return None
