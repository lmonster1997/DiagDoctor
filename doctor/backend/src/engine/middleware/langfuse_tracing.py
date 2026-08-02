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

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        # Trace start/end is owned by diagnosis_agent_node (the handler is a
        # shared singleton, so per-diagnosis trace_id/session_id are set there
        # via set_external_session_id + start_trace). This hook only logs that
        # the middleware entered the run; tool spans are recorded in
        # awrap_tool_call.
        ctx = runtime.context
        if ctx is None:
            return None
        logger.info(
            "langfuse_tracing_before_agent",
            case_id=ctx.case_id,
            langfuse_trace_id=ctx.langfuse_handler.trace_id if ctx.langfuse_handler else None,
        )
        return None

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        ctx = request.runtime.context if request.runtime is not None else None
        tool_name = request.tool.name if request.tool else "unknown"
        t0 = time.monotonic()

        result = await handler(request)

        elapsed_ms = (time.monotonic() - t0) * 1000
        content_str, _ = _extract_result_content(result)

        if ctx is not None and ctx.langfuse_handler is not None:
            # request.tool_call may be a dict or an object (LangChain varies)
            tc = request.tool_call
            tool_args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
            with contextlib.suppress(Exception):
                ctx.langfuse_handler.record_tool_span(
                    tool_name=tool_name,
                    tool_args=tool_args,
                    result=content_str[:2000],
                    latency_ms=round(elapsed_ms, 1),
                    iteration=ctx.model_call_count,
                )

        return result

    async def aafter_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        # 不在此 end_trace：trace 的 output（diagnosis report）由
        # diagnosis_agent_node 在 agent.ainvoke 返回后生成，只有它能填。
        # 此处若调 end_trace()（无 output）会把 handler._trace_id 重置为 None，
        # 而 aafter_agent 在 agent.ainvoke 返回前执行，先于 node 的
        # _finalize_langfuse_trace -> end_trace(output_data=report)，导致后者
        # 命中 ``if self._trace_id is None: return`` 直接跳过 -> trace output
        # 永远为空。故 trace 结束由 node 独占（正常 _finalize_langfuse_trace +
        # 异常 except 两条路径都调 end_trace）；start_trace 也由 node 在
        # ainvoke 前调（handler 是共享单例，per-diagnosis trace_id 在那设）。
        return None
