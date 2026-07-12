"""LangfuseTracingMiddleware — owns the Langfuse trace lifecycle + tool span recording.

Maps to the hand-written loop's Langfuse integration:
- ``abefore_agent`` ← ``node.py:79-82`` (start_trace; budget init moved to AgentLifecycleMiddleware)
- ``awrap_tool_call`` ← ``react_loop.py:139-188`` (tool latency / record_tool_span)
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

from src.graph.nodes.diagnosis_agent.run_context import (
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
    """Langfuse trace lifecycle + per-tool span recording.

    Budget / counter initialisation is owned by AgentLifecycleMiddleware
    (registered 1st).  This middleware only handles trace start and
    tool span recording.

    All Langfuse handler calls are wrapped in ``contextlib.suppress(Exception)``
    — Langfuse outages must never block a diagnosis (graceful degradation,
    same policy as the hand-written loop).
    """

    def __init__(self) -> None:
        super().__init__()
        self._local_llm_count = 0  # per-invocation LLM call counter

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        self._local_llm_count = 0  # reset per invocation
        ctx = get_run_context_or_none()
        if ctx is None:
            # create_agent() may fire abefore_agent during graph compilation
            # before diagnosis_agent_node sets the ContextVar. Silently skip.
            return None

        # Budget / counter initialisation is owned by AgentLifecycleMiddleware
        # (registered 1st — runs before this middleware).

        if ctx.langfuse_handler is not None:
            # When an external session_id is provided (experiment runner),
            # inform the handler so any trace it creates uses that session.
            if ctx.langfuse_session_id:
                ctx.langfuse_handler.set_external_session_id(ctx.langfuse_session_id)
            with contextlib.suppress(Exception):
                ctx.langfuse_handler.start_trace(
                    input_data={"evidence": ctx.evidence_text[:500]},
                    trace_id=ctx.langfuse_trace_id,
                )
        logger.info(
            "langfuse_tracing_before_agent",
            case_id=ctx.case_id,
            langfuse_trace_id=ctx.langfuse_trace_id,
            langfuse_session_id=ctx.langfuse_session_id,
            handler_trace_id=ctx.langfuse_handler.trace_id if ctx.langfuse_handler else None,
            budget_tokens=ctx.ctx_budget.total_used,
        )
        return None

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        """Record model call latency (Langfuse callbacks fire via agent.ainvoke config).

        LLM generation recording is handled by the callback path
        (on_chat_model_start → on_llm_end) registered at agent.ainvoke
        config level — NOT via model.with_config, which does not reliably
        propagate callbacks inside LangGraph's create_agent model node.
        Tool callbacks are no-ops, so no double-recording risk.
        """
        self._local_llm_count += 1

        t0 = time.monotonic()
        result = await handler(request)
        latency_ms = (time.monotonic() - t0) * 1000

        logger.debug(
            "langfuse_model_call_completed",
            idx=self._local_llm_count,
            latency_ms=round(latency_ms, 1),
        )
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

        # Record tool span to Langfuse (token accounting moved to BudgetGuard).
        # ToolDedup short-circuits dup calls before reaching here.
        if ctx is not None and ctx.langfuse_handler is not None:
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
