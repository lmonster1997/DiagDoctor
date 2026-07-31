"""§7.2 hypothesis-logging instrumentation tool.

The diagnosis agent is a native tool-calling agent (``create_agent`` +
``bind_tools``); its reasoning-step ``AIMessage.content`` is empty or natural
language, NEVER the free-text JSON hypothesis blocks the original §7.2 design
scanned for. So ``extract_findings`` never saw ``{"hypothesis":...}`` blocks and
the hypothesis tree was inert at runtime (always degraded to confirmed-only).

This tool fixes that by giving the agent a *native* channel to record
hypotheses: a no-op ``record_hypothesis`` tool it calls as it confirms/excludes
hypotheses. ``extract_findings`` then parses the hypothesis from
``msg.tool_calls`` (the reliable structured channel for a tool-calling agent)
instead of scanning free-text content.

The tool itself does nothing -- it returns a short ack. The ``Finding`` is
materialised on the parser side from the tool-call args (see ``extract_findings``).
Budget-exempt (see ``BudgetGuardMiddleware``): instrumentation must not eat the
diagnostic ``MAX_MODEL_CALLS`` cap, or the §5.3 calibration (P90=12 + 4 buffer)
distorts.
"""

from __future__ import annotations

from typing import Literal

from langchain_core.tools import StructuredTool

RECORD_HYPOTHESIS_TOOL_NAME = "record_hypothesis"


async def record_hypothesis(
    hypothesis: str,
    status: Literal["confirmed", "excluded", "pending"],
    evidence: str = "",
    refuted: bool = False,
) -> str:
    """记录一个根因假设的验证状态(确认 / 排除 / 待验证)。

    每次你**确认**或**排除**一个假设时调用此工具(先尝试找反例推翻,再确认)。
    这是埋点工具:调用本身不查任何东西,只把假设状态记下来,供续查时整理成
    "已确认/已排除/待验证"三段注入,避免你重复走已被推翻的死路。

    Args:
        hypothesis: 一句话根因假设(说清机制,如 "创建评论未校验 task_id 存在性")。
        status: ``confirmed`` 已确认 / ``excluded`` 已排除 / ``pending`` 待验证。
        evidence: 支撑证据或反例。status=excluded 时写推翻它的反例。
        refuted: 是否被反例推翻。status=excluded 时应为 true。

    Returns:
        简短 ack(假设记录由解析侧从本次工具调用参数提取,工具不持久化)。
    """
    return f"已记录假设[{status}]: {hypothesis[:80]}"


RECORD_HYPOTHESIS_TOOL = StructuredTool.from_function(
    coroutine=record_hypothesis,
    name=RECORD_HYPOTHESIS_TOOL_NAME,
    description=(
        "记录一个根因假设的验证状态(确认/排除/待验证)。每次确认或排除一个假设时调用——"
        "先尝试找反例推翻假设,再确认。status=excluded 时 refuted=true、evidence 写反例;"
        "最终 root_cause 对应 status=confirmed 的假设。"
        "这是埋点工具(不查询任何数据),仅记录假设状态供续查复用,避免重复走死路。"
    ),
)
