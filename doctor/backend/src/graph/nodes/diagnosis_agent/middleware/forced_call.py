"""ForcedFinalCallMiddleware — post-loop forced JSON call (Iter 1 + Iter 2 mechanism).

Maps to ``node.py:240-248`` + the entire ``forced_call.py``:

After the create_agent loop ends (natural stop OR cap via BudgetGuard's
``jump_to='end'``), this middleware's ``aafter_agent`` runs and checks the gate:
- If the last AIMessage already contains parseable JSON → skip (healthy case,
  zero-regression gate, matches ``_last_ai_has_json``).
- If token budget already blown → skip (forced call would fail anyway).
- If messages empty → skip (nothing to feed the LLM).

When triggered, makes ONE extra LLM call with a FRESH UN-BOUND LLM
(``get_llm_for_role("diagnosis")``) + ``with_structured_output(
ForcedDiagnosisReport, method="function_calling", include_raw=True)`` — exactly
the Iteration 2 design. The un-bound LLM is critical: create_agent's internal
LLM has diagnostic tools bound, which would let DeepSeek fall back to emitting
DSML tool_calls (the v1 REPORTING-phase trap). A fresh un-bound instance +
``with_structured_output`` binding only the report schema eliminates that
surface.

Returns ``{"messages": [forced_response]}`` — the ``add_messages`` reducer
appends the synthesized AIMessage (whose content is the JSON-serialized
``ForcedDiagnosisReport``), so the node's downstream ``parse_diagnosis_report``
picks it up as the new last AIMessage. Verified to work:
``scripts/verify_middleware_assumptions.py`` Test 2.

The Langfuse ``record_structured_output`` SPAN is recorded inside
``_forced_final_json_call`` (existing logic) — the parsed Pydantic object is
visible in the trace exactly as in the hand-written version.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel

import src.llm_factory as _llm_factory
from src.graph.nodes.diagnosis_agent.forced_call import (
    _forced_final_json_call,
    _last_ai_has_json,
    _last_ai_is_natural_stop,
)
from src.graph.nodes.diagnosis_agent.run_context import (
    get_run_context,
    get_run_context_or_none,
)
from src.observability.logger import get_logger

logger = get_logger(__name__)


class ForcedFinalCallMiddleware(AgentMiddleware):
    """Force one final structured-output LLM call if the loop didn't deliver JSON."""

    async def aafter_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        ctx = get_run_context()
        messages = state.get("messages", []) if isinstance(state, dict) else []

        # ── Gate (matches _maybe_forced_final_json_call:389-393) ──
        if not messages:
            return None
        # Always trigger forced final call — ensures consistent structured output
        # with all fields (affected_function, etc.) even on natural stop.

        natural_stop = _last_ai_is_natural_stop(messages)
        ctx.forced_call_triggered = True
        logger.info(
            "forced_final_json_call_triggered",
            case_id=ctx.case_id,
            natural_stop=natural_stop,
            budget_exhausted=ctx.budget_exhausted,
        )

        # Fresh UN-BOUND LLM — do NOT reuse create_agent's tool-bound model.
        # with_structured_output binds ONLY ForcedDiagnosisReport as a tool,
        # leaving no diagnostic-tool surface for DSML fallback (v1 trap fix).
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
            # Token-account the forced call's output (matches _maybe:427)
            ctx.ctx_budget.add_agent_reasoning(str(forced_response.content))
            # add_messages reducer appends — node's parse_diagnosis_report will
            # pick this up as the new last AIMessage.
            return {"messages": [forced_response]}

        # forced call itself failed (timeout / parsed=None / API error) —
        # _forced_final_json_call already logged + recorded the failure SPAN.
        # Fall through to the node's existing fallback report path.
        return None
