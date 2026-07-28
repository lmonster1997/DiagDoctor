"""ContextElisionMiddleware - 运行时旧工具结果符号占位(§7.1 / L2 可重取占位)。

与 ``ToolTruncationMiddleware``(入口截断,``awrap_tool_call``)正交:本中间件在
``abefore_model`` 把 N 轮前的旧 ToolMessage 替换成带重取入口的占位
(``elision.build_elision_placeholder``),**同 id 原位替换**。

替换机制(已验证,langchain 1.2.13 / langchain_core 1.4.8):``add_messages``
reducer 对同 id 的消息做**原位替换**(不重复)。create_agent 循环里 ToolMessage
经 reducer 时已被分配 uuid,故 ``abefore_model`` 读到的 ``original.id`` 非空,
返回 ``ToolMessage(id=original.id, content=占位, ...)`` 即原位替换。

**只动 ToolMessage**;SystemMessage/HumanMessage/AIMessage 不碰(保证据链 +
tool_call 结构,agent 仍知"调过什么")。占位带重取入口,agent 需要时重调工具
同参查询即可重水合--数据可寻址故不丢信息(见 ``docs/L1-L4_Loki_Tempo_适用性分析.md``)。

判龄:ToolMessage 倒序排名(最近 rank=0);``rank >= keep_recent`` 的替换。
``keep_recent`` 默认 3(``settings.context_elision_keep_recent``),未标定(参考
旧 compaction keep_recent=4 + §5.3 P90=12 轮;后续用 ``analyze_budget.py`` 看替换率再调)。
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from src.config import settings
from src.engine.context.elision import build_elision_placeholder
from src.observability.logger import get_logger

logger = get_logger(__name__)


class ContextElisionMiddleware(AgentMiddleware):
    """Replace aged ToolMessages with re-fetchable placeholders before each model call.

    Registered after ToolTruncation (entry truncation) and before BudgetGuard,
    so the model sees elided context and the budget gate's real-usage reflects
    the post-elision size.
    """

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        if not settings.context_elision_enabled:
            return None

        messages: list[BaseMessage] = (
            state.get("messages", []) if isinstance(state, dict) else []
        )
        if not messages:
            return None

        keep_recent = settings.context_elision_keep_recent

        # 倒序收集 ToolMessage 索引;rank 0 = 最近一次工具结果。
        tool_msg_indices: list[int] = [
            i for i in range(len(messages) - 1, -1, -1)
            if isinstance(messages[i], ToolMessage)
        ]

        replacements: list[ToolMessage] = []
        for rank, idx in enumerate(tool_msg_indices):
            if rank < keep_recent:
                continue  # 近 keep_recent 条保留原文
            original = messages[idx]
            if not isinstance(original, ToolMessage):  # 防御(race 上限已不必要)
                continue

            tool_name = original.name or "unknown"
            tool_call_args = _find_tool_call_args(messages, original.tool_call_id)
            placeholder = build_elision_placeholder(
                tool_name, tool_call_args, str(original.content)
            )
            replacements.append(
                ToolMessage(
                    content=placeholder,
                    tool_call_id=original.tool_call_id,
                    name=original.name,
                    id=original.id,  # 同 id -> add_messages 原位替换(已验证)
                )
            )

        if not replacements:
            return None

        logger.info(
            "context_elision_applied",
            total_tool_msgs=len(tool_msg_indices),
            elided=len(replacements),
            keep_recent=keep_recent,
        )
        return {"messages": replacements}


def _find_tool_call_args(
    messages: list[BaseMessage], tool_call_id: str | None
) -> dict[str, Any]:
    """找前一条匹配 ``tool_call_id`` 的 AIMessage,取其 tool_call ``args``。

    用于构造非 obs 工具的重取入口(obs 优先从结果 JSON 取 echo 的 query/time_range,
    JSON 不可解析时也回退到这里)。
    """
    if not tool_call_id:
        return {}
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            if not isinstance(tc, dict):
                continue
            if tc.get("id") == tool_call_id:
                args = tc.get("args", {})
                return args if isinstance(args, dict) else {}
    return {}
