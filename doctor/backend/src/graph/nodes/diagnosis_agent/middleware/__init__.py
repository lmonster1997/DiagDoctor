"""DiagnosisAgent middleware package — 5 middlewares for the create_agent loop.

Replaces the hand-written ReAct loop in ``react_loop.py`` with langchain
``create_agent`` + middleware. Each middleware owns one concern from the
case-driven harness iteration log:

- ``LangfuseTracingMiddleware`` — start/end trace + record_tool_span (Iter 0 baseline)
- ``BudgetGuardMiddleware`` — MAX_TOOL_CALLS / token / time caps → jump_to end (Iter 0 baseline)
- ``ToolDedupMiddleware`` — skip identical repeated tool calls (Iter 0 baseline)
- ``ToolTruncationMiddleware`` — static per-tool result truncation (Iter 0 baseline)
- ``ForcedFinalCallMiddleware`` — post-loop forced JSON call with un-bound LLM
  + with_structured_output (Iter 1 + Iter 2)

Per-invocation state lives in a ``ContextVar`` (``DiagnosisRunContext``) set by
``diagnosis_agent_node`` before ``agent.ainvoke`` — middleware instances are
reused across invocations so ``self`` must stay stateless.

Hook signatures (langchain 1.3.11, verified from source):
- state hooks: ``async def axxx(self, state, runtime) -> dict[str, Any] | None``
- ``awrap_tool_call(self, request, handler) -> ToolMessage | Command``
- ``awrap_model_call(self, request, handler) -> ModelResponse | AIMessage | ExtendedModelResponse``

Verified assumptions (scripts/verify_middleware_assumptions.py):
- wrap_tool_call registration order = outer->inner (first registered wraps outermost)
- after_agent {"messages": [AIMessage]} IS appended to ainvoke() result
- config={"callbacks": [handler]} propagates to internal LLM calls
"""

from __future__ import annotations

from src.graph.nodes.diagnosis_agent.middleware.budget_guard import BudgetGuardMiddleware
from src.graph.nodes.diagnosis_agent.middleware.forced_call import ForcedFinalCallMiddleware
from src.graph.nodes.diagnosis_agent.middleware.langfuse_tracing import LangfuseTracingMiddleware
from src.graph.nodes.diagnosis_agent.run_context import (
    DiagnosisRunContext,
    clear_run_context,
    get_run_context,
    set_run_context,
)
from src.graph.nodes.diagnosis_agent.middleware.tool_dedup import ToolDedupMiddleware
from src.graph.nodes.diagnosis_agent.middleware.tool_truncation import ToolTruncationMiddleware

__all__ = [
    "DiagnosisRunContext",
    "set_run_context",
    "get_run_context",
    "clear_run_context",
    "LangfuseTracingMiddleware",
    "BudgetGuardMiddleware",
    "ToolDedupMiddleware",
    "ToolTruncationMiddleware",
    "ForcedFinalCallMiddleware",
]
