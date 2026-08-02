"""BudgetGuardMiddleware — MAX_MODEL_CALLS / token / time caps → jump_to end.

Registered 5th in the middleware pipeline (before ForcedFinalCall).
Uses ``@hook_config(can_jump_to=["end"])`` to stop the agent loop
when any budget dimension is exhausted.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, BaseMessage

from src.engine.budget.constants import MAX_MODEL_CALLS, MAX_TIME_SECONDS, MAX_TOKENS_BUDGET
from src.observability.logger import get_logger

logger = get_logger(__name__)


class BudgetGuardMiddleware(AgentMiddleware):
    """Enforce MAX_MODEL_CALLS / token / time hard caps via jump_to='end'."""

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        ctx = runtime.context
        if ctx is None:
            return None
        ctx.model_call_count += 1
        ctx.ctx_budget.tick_iteration()

        if ctx.model_call_count > MAX_MODEL_CALLS:
            ctx.budget_exhausted = True
            logger.warning(
                "budget_iteration_cap_hit",
                case_id=ctx.case_id,
                model_call_count=ctx.model_call_count,
                max=MAX_MODEL_CALLS,
            )
            return {"jump_to": "end"}

        if (
            ctx.ctx_budget.total_used >= MAX_TOKENS_BUDGET
            or ctx.ctx_budget.elapsed_seconds >= MAX_TIME_SECONDS
        ):
            ctx.budget_exhausted = True
            logger.warning(
                "budget_hard_limit_hit",
                case_id=ctx.case_id,
                model_call_count=ctx.model_call_count,
                total_used=ctx.ctx_budget.total_used,
                elapsed_seconds=round(ctx.ctx_budget.elapsed_seconds, 1),
            )
            return {"jump_to": "end"}

        return None

    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        ctx = runtime.context
        if ctx is None:
            return None
        messages: list[BaseMessage] = state.get("messages", []) if isinstance(state, dict) else []
        for msg in reversed(messages):
            if isinstance(msg, AIMessage):
                ctx.ctx_budget.add_agent_reasoning(str(msg.content))
                # §7.3 接真实 usage：peak input_tokens 是模型实际收到的 context
                # 大小（截断后口径），作为 gate 的 token 主口径，根治 §6.1
                # split-brain（不再依赖 tiktoken 估算截断前 tool result）。
                usage = getattr(msg, "usage_metadata", None)
                if isinstance(usage, dict) and usage.get("input_tokens"):
                    ctx.ctx_budget.record_real_usage(int(usage["input_tokens"]))
                # §7.2: record_hypothesis 是埋点工具,纯埋点 turn(所有 tool_calls
                # 都是 record_hypothesis)不推进 model_call_count,保住 §5.3 的
                # MAX_MODEL_CALLS=16 诊断标定不被埋点吃掉。token 口径照常计
                # (埋点 turn 确实消耗了 token,是真实开销)。
                tcs = getattr(msg, "tool_calls", None) or []
                if tcs and all(
                    (tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", ""))
                    == "record_hypothesis"
                    for tc in tcs
                ):
                    ctx.model_call_count = max(0, ctx.model_call_count - 1)
                break
        return None

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        ctx = request.runtime.context if request.runtime is not None else None
        result = await handler(request)
        if ctx is not None:
            try:
                tc = getattr(request, "tool_call", None)
                tc_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
                # §7.2: record_hypothesis 是埋点工具,不计入 tool_calls/tool_result 预算口径,
                # 否则会吃掉诊断 MAX_MODEL_CALLS cap、扭曲 §5.3 标定(P90=12 + 4 buffer)。
                if tc_name != "record_hypothesis":
                    ctx.ctx_budget.add_tool_call(1)
                    content = str(getattr(result, "content", result))
                    # tool_result 的 tiktoken 估算仅作 to_dict telemetry breakdown，
                    # 不进 gate（gate 用 aafter_model 里记录的 real_input_tokens）。
                    # 故无需像旧 §6.1 临时止血那样在此 truncate -- 真实 usage 本就是
                    # 截断后口径，split-brain 已彻底修。
                    ctx.ctx_budget.add_tool_result(content)
            except Exception:
                pass
        return result
