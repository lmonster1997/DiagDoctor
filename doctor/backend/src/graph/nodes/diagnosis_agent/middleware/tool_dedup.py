"""ToolDedupMiddleware — skip identical repeated tool calls.

Maps to ``react_loop.py:113-135``: build ``call_key = (tool_name,
json.dumps(args, sort_keys=True))``, check against ``call_history``; on hit
return a ``[跳过]`` ToolMessage without executing, and record ``tool_skipped``
on the Langfuse handler. On miss, append to history and call the inner handler
(LangfuseTracing → ToolTruncation → real tool exec).

Registered FIRST (outermost) so dup short-circuits before Langfuse/Truncation
see the call — matches the hand-written loop's ``continue`` after recording
the skip, which never reaches ``add_tool_call`` / ``record_tool_span`` /
``truncate_tool_result`` for dup calls.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from src.graph.nodes.diagnosis_agent.run_context import (
    get_run_context,
    get_run_context_or_none,
)
from src.observability.logger import get_logger

logger = get_logger(__name__)


class ToolDedupMiddleware(AgentMiddleware):
    """Skip tool calls whose (name, args) match a prior call in this invocation."""

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        ctx = get_run_context_or_none()
        tool = request.tool
        tool_call = request.tool_call
        tool_name = tool.name if tool is not None else tool_call.get("name", "unknown")
        tool_args = tool_call.get("args", {}) if isinstance(tool_call, dict) else getattr(tool_call, "args", {})
        tool_call_id = (
            tool_call.get("id", "") if isinstance(tool_call, dict) else getattr(tool_call, "id", "")
        )

        call_key = (tool_name, json.dumps(tool_args, sort_keys=True, default=str))
        if ctx is not None and call_key in ctx.call_history:
            logger.debug(
                "tool_call_skipped_duplicate",
                tool_name=tool_name,
            )
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
            ctx.call_history.append(call_key)
        return await handler(request)
