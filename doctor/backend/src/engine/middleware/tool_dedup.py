"""ToolDedupMiddleware — skip identical repeated tool calls (elision-aware).

Registered outermost so dup short-circuits before Langfuse/Truncation see the
call. A same-(name,args) call is skipped ONLY if the prior result is still in
context; if ContextElision has aged it to a placeholder
(``ctx.elided_tool_call_ids``) the re-call is a legitimate re-fetch and is
allowed through. Without this, elision's "re-call same args to rehydrate"
affordance and dedup's "block identical calls" contract contradict -> agent
spirals on re-fetches (recursion-limit loop).
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from src.engine.run_context import get_run_context_or_none
from src.observability.logger import get_logger

logger = get_logger(__name__)


class ToolDedupMiddleware(AgentMiddleware):
    """Skip tool calls whose (name, args) match a prior call in this invocation."""

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        ctx = get_run_context_or_none()
        tool = request.tool
        tool_call = request.tool_call
        tool_name = tool.name if tool is not None else tool_call.get("name", "unknown")
        tool_args = (
            tool_call.get("args", {})
            if isinstance(tool_call, dict)
            else getattr(tool_call, "args", {})
        )
        tool_call_id = (
            tool_call.get("id", "") if isinstance(tool_call, dict) else getattr(tool_call, "id", "")
        )

        call_key = (tool_name, json.dumps(tool_args, sort_keys=True, default=str))
        if ctx is not None and call_key in ctx.call_history:
            original_tc_id = ctx.call_history[call_key]
            # elision-aware dedup: 若上次同参调用的结果已被 ContextElision ageing
            # 成占位(ctx.elided_tool_call_ids),这是合法重水合而非浪费重复 ->
            # 放行重取,并把映射更新到本次结果的 id(下次 ageing 以新 id 为准)。
            # 否则原结果仍在上下文里 -> 视作浪费重复,跳过(原行为)。这两条路径的
            # 区分依赖 ctx.elided_tool_call_ids(elision 写、本中间件读),见
            # run_context 的契约注释;联合行为由 test_middleware 的集成测锁住。
            if original_tc_id in ctx.elided_tool_call_ids:
                logger.debug(
                    "tool_call_refetch_allowed_after_elision",
                    tool_name=tool_name,
                )
                result = await handler(request)
                ctx.call_history[call_key] = tool_call_id
                return result

            logger.debug("tool_call_skipped_duplicate", tool_name=tool_name)
            if ctx.langfuse_handler is not None:
                with contextlib.suppress(Exception):
                    ctx.langfuse_handler.record_tool_skipped(
                        tool_name=tool_name,
                        tool_args=tool_args,
                        iteration=ctx.model_call_count,
                    )
            return ToolMessage(
                content="[跳过：与之前调用完全相同]",
                tool_call_id=tool_call_id,
                name=tool_name,
            )

        if ctx is not None:
            ctx.call_history[call_key] = tool_call_id

        return await handler(request)
