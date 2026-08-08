"""P1 active-clarification tool: lets the agent ask the user a question.

The diagnosis agent sometimes hits a wall on information it **cannot** obtain
through its tools -- "is this intermittent or deterministic?", "did anything
change in the config recently?", "who is the caller?". The right move is to
**ask the operator**, not flail until budget exhausts (the v1 passive fallback).
This tool gives the agent a native channel to do that proactively.

Mechanics (see ``docs/hitl-evolution-plan.md`` §4 / ``.claude/plans/p1-active-clarification.md``):
the tool itself is a pure ack -- it returns a short confirmation and records
nothing. ``ClarificationMiddleware`` detects the ``request_user_clarification``
tool_call on the next ``abefore_model`` and ``jump_to="end"`` stops the inner
ReAct loop. ``_diagnosis_agent_node`` then reads ``run_ctx.clarification_requested``
+ ``clarification_question`` and routes to the outer-graph ``clarify_input`` node,
which ``interrupt()``s (mirroring ``human_input_node``). ``Command(resume=<answer>)``
returns the operator's answer as this tool's logical result and re-enters
``diagnosis_agent`` for an informed continuation pass.

Why an ack tool and not ``interrupt()`` inside the tool: the inner
``create_agent`` subgraph is compiled **without** a checkpointer and invoked
imperatively (``agent.ainvoke`` inside a function node), so an in-tool
``interrupt()`` cannot persist/resume and cannot pause the outer graph. The
interrupt must fire at an outer-graph node boundary -- hence the dedicated
``clarify_input`` node. The tool's job is only to *signal intent*; the pause
machinery lives on the outer graph (same proven pattern as ``human_input``).

Not budget-exempt: the turn that emits this tool_call is a real reasoning turn
and counts toward ``MAX_MODEL_CALLS`` (and the loop stops right after, so the
budget resets on the resume pass anyway).
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool

REQUEST_CLARIFICATION_TOOL_NAME = "request_user_clarification"


async def request_user_clarification(question: str) -> str:
    """主动向用户提一个澄清问题(当你缺关键信息、且无法用工具获取时)。

    适用场景:你发现要继续推进诊断,缺少一类**工具拿不到**的信息,例如
    - 偶发还是必现?触发条件/频率?
    - 最近是否改过配置 / 发版 / 数据迁移?
    - 调用方是谁?只在特定环境/用户/数据下复现?
    - 期望行为是什么(确认是 bug 而非误解)?

    调用本工具后**停止调查、等待用户回复**:系统会暂停并把你的问题呈现给用户,
    用户回答后你将基于回答继续诊断(全新一轮 + 已有发现作为上下文)。

    ⚠️ 不要在还能用工具自查时滥用:仅当你确实卡在"工具拿不到的信息"上才调用。
    每次只问**一个**最关键的问题(最多 2 次澄清机会,问完即止)。

    Args:
        question: 一个聚焦的澄清问题(中文,说清你缺什么信息、为什么需要它)。

    Returns:
        简短 ack。真正的用户回答会在你下一轮(暂停恢复后)作为上下文注入,
        本工具不直接返回用户回答。
    """
    return f"已向用户提问: {question[:120]}。等待回复后继续调查。"


REQUEST_CLARIFICATION_TOOL = StructuredTool.from_function(
    coroutine=request_user_clarification,
    name=REQUEST_CLARIFICATION_TOOL_NAME,
    description=(
        "主动向用户提一个澄清问题。仅当你缺关键信息(偶发/必现?最近变更?调用方?"
        "触发条件?)且无法通过其他工具获取时调用。调用后停止调查、等待用户回复,"
        "系统会暂停并把问题呈现给用户,回答后你基于回答继续诊断。每次只问一个最"
        "关键的问题(最多 2 次机会)。勿在还能自查时滥用。"
    ),
)
