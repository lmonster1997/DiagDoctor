"""LangfuseTracingMiddleware — Langfuse trace lifecycle + tool span recording.

All Langfuse handler calls are wrapped in ``contextlib.suppress(Exception)``
— Langfuse outages must never block a diagnosis.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from src.engine.run_context import get_run_context_or_none
from src.observability.logger import get_logger

logger = get_logger(__name__)


def _extract_result_content(result: Any) -> tuple[str, ToolMessage | None]:
    if isinstance(result, ToolMessage):
        return str(result.content), result
    update = getattr(result, "update", None)
    if isinstance(update, dict):
        msgs = update.get("messages", [])
        for m in msgs:
            if isinstance(m, ToolMessage):
                return str(m.content), m
    return str(result), None


class LangfuseTracingMiddleware(AgentMiddleware):
    """Langfuse trace lifecycle + per-tool span recording."""

    def __init__(self) -> None:
        super().__init__()
        self._local_llm_count = 0

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        self._local_llm_count = 0
        ctx = get_run_context_or_none()
        if ctx is None:
            return None

        if ctx.langfuse_handler is not None:
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
        )
        return None

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        ctx = get_run_context_or_none()
        tool_name = request.tool.name if request.tool else "unknown"
        t0 = time.monotonic()

        result = await handler(request)

        elapsed_ms = (time.monotonic() - t0) * 1000
        content_str, _ = _extract_result_content(result)

        if ctx is not None and ctx.langfuse_handler is not None:
            with contextlib.suppress(Exception):
                ctx.langfuse_handler.record_tool_span(
                    tool_name=tool_name,
                    tool_args=getattr(request.tool_call, "args", {}),
                    result=content_str[:2000],
                    latency_ms=round(elapsed_ms, 1),
                    iteration=ctx.model_call_count,
                )

        return result

    async def aafter_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        ctx = get_run_context_or_none()
        if ctx is None:
            return None
        if ctx.langfuse_handler is not None:
            with contextlib.suppress(Exception):
                ctx.langfuse_handler.end_trace()
        return None
