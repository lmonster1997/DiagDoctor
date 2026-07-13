"""ToolTruncationMiddleware — static per-tool result truncation.

Registered innermost so truncation is applied to raw tool output before
LangfuseTracingMiddleware records it.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from src.engine.context.truncation import truncate_tool_result
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
            logger.warning("tool_execution_error", tool_name=tool_name, error=str(exc))
            error_str = truncate_tool_result(tool_name, f"工具执行错误: {exc}")
            return ToolMessage(content=error_str, tool_call_id=tool_call_id, name=tool_name)

        if isinstance(result, ToolMessage):
            truncated = truncate_tool_result(tool_name, str(result.content))
            if truncated == str(result.content):
                return result
            return ToolMessage(
                content=truncated,
                tool_call_id=result.tool_call_id,
                name=result.name or tool_name,
            )

        return result
