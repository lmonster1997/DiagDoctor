"""
上下文压缩 — 历史消息降级 + 自动压缩触发器。

提供：
- ``degrade_old_tool_results()`` — 旧工具消息降级
- ``maybe_compact_context()`` — 根据预算自动触发压缩
"""

from __future__ import annotations

from langchain_core.messages import BaseMessage, ToolMessage

from src.engine.context.budget import ContextBudget, estimate_tokens
from src.observability.logger import get_logger

logger = get_logger(__name__)

_COMPRESS_MARKER = "[已压缩]"


def degrade_old_tool_results(
    messages: list[BaseMessage],
    keep_recent: int = 4,
) -> list[BaseMessage]:
    """降级旧工具消息以减少 context 占用。

    策略:
    - 最近 ``keep_recent`` 条 ToolMessage：保留原文
    - 第 keep_recent+1 至 keep_recent*2 条：保留首行 + ``[已摘要]``
    - 第 keep_recent*2+1 条及更早：替换为 ``[已归档：工具 {name} 的结果已省略]``
    """
    tool_indices: list[int] = []
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], ToolMessage):
            tool_indices.append(i)

    for rank, idx in enumerate(tool_indices):
        msg = messages[idx]
        if not isinstance(msg, ToolMessage):
            continue

        original_content = str(msg.content)

        if rank < keep_recent:
            continue
        elif rank < keep_recent * 2:
            first_line = original_content.split("\n")[0]
            messages[idx] = ToolMessage(
                content=f"{first_line}\n[已摘要]",
                tool_call_id=msg.tool_call_id,
                name=msg.name,
            )
        else:
            tool_label = msg.name or "unknown"
            messages[idx] = ToolMessage(
                content=f"[已归档：工具 {tool_label} 的结果已省略]",
                tool_call_id=msg.tool_call_id,
                name=msg.name,
            )

    return messages


def maybe_compact_context(
    messages: list[BaseMessage],
    budget: ContextBudget,
) -> tuple[list[BaseMessage], bool]:
    """根据预算自动压缩 context。

    触发条件:
    - usage_ratio > 60%: 调用 ``degrade_old_tool_results(keep_recent=3)``
    - usage_ratio > 75%: 额外将所有工具结果截断到 500 字符以内
    """
    compacted = False

    if budget.is_warning():
        original_len = sum(
            estimate_tokens(str(m.content)) for m in messages if hasattr(m, "content")
        )
        messages = degrade_old_tool_results(messages, keep_recent=3)
        new_len = sum(estimate_tokens(str(m.content)) for m in messages if hasattr(m, "content"))
        compacted = True
        logger.info(
            "context_compacted_warning",
            phase=budget.phase.value,
            before_tokens=original_len,
            after_tokens=new_len,
            reduction_pct=round((1 - new_len / original_len) * 100, 1) if original_len else 0,
        )

    if budget.usage_ratio > 0.75:
        for i, msg in enumerate(messages):
            if isinstance(msg, ToolMessage):
                content = str(msg.content)
                if len(content) > 500:
                    messages[i] = ToolMessage(
                        content=content[:500] + f"\n{_COMPRESS_MARKER}",
                        tool_call_id=msg.tool_call_id,
                        name=msg.name,
                    )
        compacted = True
        logger.info(
            "context_compacted_critical",
            phase=budget.phase.value,
            usage_ratio=budget.usage_ratio,
        )

    return messages, compacted
