"""ToolTruncationMiddleware — static per-tool result truncation.

Maps to ``react_loop.py:138-155``: catch tool execution errors and stringify
them (so a single tool failure doesn't crash the loop), then apply
``truncate_tool_result(tool_name, str(result))`` to prevent one verbose result
from blowing out the context window.

Registered LAST (innermost) so truncation is applied to the raw tool output
before LangfuseTracingMiddleware (middle) records it — matches the hand-written
loop where ``result_str = truncate_tool_result(...)`` is computed BEFORE
``record_tool_span(result=result_str)`` and ``add_tool_result(result_str)``.

Catches ``Exception`` from the inner handler (the actual tool exec) and turns
it into a ``ToolMessage`` with the error string — matches
``react_loop.py:141-151``. If the langgraph ToolNode already catches tool
errors itself, this try/except is a no-op (harmless safety net).
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from src.graph.context_engine import truncate_tool_result
from src.observability.logger import get_logger

logger = get_logger(__name__)


class ToolTruncationMiddleware(AgentMiddleware):
    """Truncate tool results to per-tool character caps; catch tool exceptions."""

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        tool = request.tool
        tool_call = request.tool_call
        tool_name = tool.name if tool is not None else tool_call.get("name", "unknown")
        tool_call_id = (
            tool_call.get("id", "") if isinstance(tool_call, dict) else getattr(tool_call, "id", "")
        )

        try:
            result = await handler(request)
        except Exception as exc:
            logger.warning(
                "tool_execution_error",
                tool_name=tool_name,
                error=str(exc),
            )
            error_str = truncate_tool_result(tool_name, f"工具执行错误: {exc}")
            return ToolMessage(
                content=error_str,
                tool_call_id=tool_call_id,
                name=tool_name,
            )

        # If the handler returned a ToolMessage, truncate its content in place
        # (return a new ToolMessage — ToolMessage is somewhat immutable in
        # practice and we don't want to mutate langgraph's internal state).
        if isinstance(result, ToolMessage):
            truncated = truncate_tool_result(tool_name, str(result.content))
            if truncated == str(result.content):
                return result
            return ToolMessage(
                content=truncated,
                tool_call_id=result.tool_call_id,
                name=result.name or tool_name,
            )

        # Command case — extract ToolMessage, truncate, rebuild. Less common;
        # keep the Command shape but swap the message content.
        update = getattr(result, "update", None)
        if isinstance(update, dict):
            msgs = update.get("messages", [])
            new_msgs: list[Any] = []
            changed = False
            for m in msgs:
                if isinstance(m, ToolMessage):
                    orig = str(m.content)
                    trunc = truncate_tool_result(tool_name, orig)
                    if trunc != orig:
                        changed = True
                        new_msgs.append(
                            ToolMessage(
                                content=trunc,
                                tool_call_id=m.tool_call_id,
                                name=m.name or tool_name,
                            )
                        )
                    else:
                        new_msgs.append(m)
                else:
                    new_msgs.append(m)
            if changed:
                try:
                    return result.__class__(
                        **{**vars(result), "update": {**update, "messages": new_msgs}}
                    )
                except Exception:
                    # If rebuilding the Command fails, fall through and return
                    # the original (un-truncated) — better than crashing the loop.
                    logger.warning("truncate_command_rebuild_failed", tool_name=tool_name)
        return result
