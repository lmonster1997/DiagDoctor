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
        # 记录被 ageing 的 tool_call_id 供 ToolDedupMiddleware 放行重取(见
        # DiagnosisRunContext.elided_tool_call_ids 契约注释)。ctx 经 runtime.context
        # 注入,单测里传带 context 的 Runtime;context 未注入时为 None(跳过记录)。
        ctx = runtime.context

        # 倒序收集 ToolMessage 索引;rank 0 = 最近一次工具结果。
        tool_msg_indices: list[int] = [
            i for i in range(len(messages) - 1, -1, -1)
            if isinstance(messages[i], ToolMessage)
        ]

        # 一次扫描建 tool_call_id -> args 索引,占位构造 O(1) 取重取入口
        # (替代每个 aged ToolMessage 都 O(M) 重扫全表;整轮 O(E·M)->O(M+E))。
        tc_args_by_id = _index_tool_call_args(messages)

        replacements: list[ToolMessage] = []
        for rank, idx in enumerate(tool_msg_indices):
            if rank < keep_recent:
                continue  # 近 keep_recent 条保留原文
            original = messages[idx]
            if not isinstance(original, ToolMessage):  # 防御(race 上限已不必要)
                continue

            # 已归档过(prior pass 的同 id 原位替换)-> 跳过:省重扫/重建占位,且防
            # "关键发现"退化(再跑 build_elision_placeholder 会把占位首行=handle 行
            # 当 finding,丢掉原始关键发现)。elided_tool_call_ids:本中间件写、
            # ToolDedupMiddleware 读(见 run_context 契约注释)。ctx 未设(单测)则不跳。
            if ctx is not None and original.tool_call_id in ctx.elided_tool_call_ids:
                continue

            if ctx is not None:
                ctx.elided_tool_call_ids.add(original.tool_call_id)
            tool_name = original.name or "unknown"
            tool_call_args = tc_args_by_id.get(original.tool_call_id, {})
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
            elided=len(replacements),  # 本轮新归档数(已归档的跳过不计)
            keep_recent=keep_recent,
        )
        return {"messages": replacements}


def _index_tool_call_args(messages: list[BaseMessage]) -> dict[str, dict[str, Any]]:
    """扫一次所有 AIMessage,建 ``tool_call_id -> args`` 索引(供 O(1) 查)。

    用于构造非 obs 工具的重取入口(obs 优先从结果 JSON 取 echo 的 query/time_range,
    JSON 不可解析时也回退到这里)。原 ``_find_tool_call_args`` 每个 aged ToolMessage
    都 O(M) 重扫全表;索引法把整轮 abefore_model 从 O(E·M) 降到 O(M + E)。
    """
    index: dict[str, dict[str, Any]] = {}
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            if not isinstance(tc, dict):
                continue
            tc_id = tc.get("id")
            if not tc_id:
                continue
            index[tc_id] = tc.get("args", {})
    return index
