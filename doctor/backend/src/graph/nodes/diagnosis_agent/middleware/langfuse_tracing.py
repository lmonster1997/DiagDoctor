"""LangfuseTracingMiddleware — owns the Langfuse trace lifecycle + tool span recording.

Maps to the hand-written loop's Langfuse integration:
- ``abefore_agent`` ← ``node.py:79-82`` (start_trace) + ``react_loop.py:88`` (ContextBudget init)
- ``awrap_tool_call`` ← ``react_loop.py:139-188`` (tool latency / record_tool_span / add_tool_call / add_tool_result)
- ``aafter_agent`` ← ``node.py:149-156`` (end_trace)

Registered SECOND in the middleware list (after ToolDedup, before ToolTruncation)
so ``awrap_tool_call`` wraps OUTSIDE truncation — it sees the truncated result
(matches ``react_loop.py`` where ``record_tool_span(result=result_str)`` gets
the already-truncated ``result_str``). ToolDedup wraps outside this, so dup
calls short-circuit before this middleware runs (this middleware never records
a span for skipped dups — that's ToolDedup's ``record_tool_skipped`` job).

Langfuse LLM generation observations (on_llm_start/end) come from the callback
handler attached via ``config={"callbacks": [handler]}`` at ``agent.ainvoke`` —
verified to propagate to internal LLM calls
(scripts/verify_middleware_assumptions.py Test 3). This middleware does NOT
need to manually attach callbacks to model calls.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from src.graph.context_engine import ContextBudget
from src.graph.nodes.diagnosis_agent.middleware.run_context import (
    DiagnosisRunContext,
    get_run_context,
    get_run_context_or_none,
)
from src.observability.logger import get_logger

logger = get_logger(__name__)


def _extract_result_content(result: Any) -> tuple[str, ToolMessage | None]:
    """Extract the textual content + the ToolMessage from a wrap_tool_call result.

    The handler may return a ``ToolMessage`` or a ``Command`` wrapping messages.
    Returns ``(content_str, tool_message_or_none)`` so the caller can record the
    span and the caller's caller can return the message unchanged.
    """
    if isinstance(result, ToolMessage):
        return str(result.content), result
    # Command case — extract first ToolMessage from update
    update = getattr(result, "update", None)
    if isinstance(update, dict):
        msgs = update.get("messages", [])
        for m in msgs:
            if isinstance(m, ToolMessage):
                return str(m.content), m
    # Fallback: stringify
    return str(result), None


class LangfuseTracingMiddleware(AgentMiddleware):
    """Langfuse trace lifecycle + per-tool span recording + tool token accounting.

    Initializes the run context's ``ctx_budget`` (system prompt + evidence
    tokens) and ``call_history`` in ``abefore_agent`` so downstream middlewares
    (BudgetGuard, ToolDedup) see a fresh budget per invocation.

    All Langfuse handler calls are wrapped in ``contextlib.suppress(Exception)``
    — Langfuse outages must never block a diagnosis (graceful degradation,
    same policy as the hand-written loop).
    """

    def __init__(self) -> None:
        super().__init__()
        self._local_llm_count = 0  # per-invocation LLM call counter

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        self._local_llm_count = 0  # reset per invocation

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        self._local_llm_count = 0  # reset per invocation
        ctx = get_run_context()
        # Fresh per-invocation budget + dedup cache
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

        if ctx.langfuse_handler is not None:
            with contextlib.suppress(Exception):
                ctx.langfuse_handler.start_trace(
                    input_data={"evidence": ctx.evidence_text[:500]},
                    trace_id=ctx.langfuse_trace_id,
                )
        logger.debug(
            "langfuse_tracing_before_agent",
            case_id=ctx.case_id,
            budget_tokens=ctx.ctx_budget.total_used,
        )
        return None

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        """Attach the Langfuse handler to THIS LLM call only (not tool calls).

        Dual strategy: callback propagation (via with_config) + explicit fallback
        recording for providers where callback propagation fails.
        """
        ctx = get_run_context_or_none()
        handler_ok = ctx is not None and ctx.langfuse_handler is not None
        if not handler_ok:
            logger.debug("awrap_model_call_no_handler", has_ctx=ctx is not None)

        if handler_ok:
            try:
                request.model = request.model.with_config(
                    {"callbacks": [ctx.langfuse_handler]}
                )
            except Exception:
                pass

        # Invoke model — use local counter (ctx.model_call_count is stale)
        self._local_llm_count += 1
        idx = self._local_llm_count
        t0 = time.monotonic()
        result = await handler(request)
        latency_ms = (time.monotonic() - t0) * 1000

        # Explicit fallback: record generation
        if handler_ok and ctx is not None:
            try:
                ctx.langfuse_handler.record_llm_generation(
                    name=f"llm_call_{idx}",
                    model=getattr(request.model, "model_name", "unknown"),
                    input_data={"messages_preview": str(getattr(request, "messages", ""))[:2000]},
                    output_data={"content_preview": str(getattr(result, "content", ""))[:2000]},
                    latency_ms=latency_ms,
                )
            except Exception as exc:
                logger.warning("record_llm_generation_failed", idx=idx, error=str(exc))

        return result

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        ctx = get_run_context_or_none()
        tool = request.tool
        tool_name = tool.name if tool is not None else "unknown"
        tool_call = request.tool_call
        tool_args = tool_call.get("args", {}) if isinstance(tool_call, dict) else getattr(tool_call, "args", {})
        iteration = ctx.model_call_count if ctx else 0

        tool_t0 = time.monotonic()
        result = await handler(request)
        latency_ms = (time.monotonic() - tool_t0) * 1000

        result_str, _ = _extract_result_content(result)

        # Tool token accounting (matches react_loop.py:156-170 — only for
        # non-dup calls; ToolDedup short-circuits dup calls before reaching here)
        if ctx is not None:
            ctx.ctx_budget.add_tool_call(1)
            ctx.ctx_budget.add_tool_result(result_str)
            if ctx.langfuse_handler is not None:
                with contextlib.suppress(Exception):
                    ctx.langfuse_handler.record_tool_span(
                        tool_name=tool_name,
                        tool_args=tool_args,
                        result=result_str,
                        latency_ms=latency_ms,
                        iteration=iteration,
                        error=None,
                    )
        return result

    # NOTE: end_trace is owned by diagnosis_agent_node (_finalize_langfuse_trace),
    # NOT by this middleware. The node runs after agent.ainvoke() returns and
    # has access to the parsed DiagnosisReport — including it in end_trace
    # output_data is what the hand-written loop did. Middleware's aafter_agent
    # runs inside ainvoke() before the report is parsed, so it cannot include
    # the report. Keeping a single end_trace owner avoids double-close.
