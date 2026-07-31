"""预算状态追踪 — update_budget + is_budget_exceeded。

用于 CopilotKit 路径中的诊断节点后处理。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import tiktoken
from langchain_core.messages import AIMessage

from src.engine.budget.constants import MAX_MODEL_CALLS, MAX_TIME_SECONDS, MAX_TOKENS_BUDGET
from src.engine.state import BudgetState

_encoder = tiktoken.get_encoding("cl100k_base")


def estimate_tokens(text: str) -> int:
    """Estimate token count using cl100k_base (OpenAI-compatible models)."""
    return len(_encoder.encode(text))


def update_budget(budget: BudgetState, agent_result: dict[str, Any]) -> BudgetState:
    """Update budget state from agent execution result.

    Counts tool calls and accumulates token usage from messages. Prefers real
    ``usage_metadata.total_tokens`` from AIMessages (§7.3 接真实 usage); falls back
    to tiktoken estimate of message content when usage_metadata is absent (mock /
    provider quirk). Real usage makes the trace's ``total_tokens`` accurate.
    """
    messages: list[Any] = agent_result.get("messages", [])
    tool_call_count = 0
    real_total_tokens = 0
    has_real_usage = False

    for msg in messages:
        if isinstance(msg, AIMessage):
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                # §7.2: record_hypothesis 是埋点工具,不计入诊断 tool_calls 口径
                # (与 BudgetGuardMiddleware 豁免一致;否则 record 调用会顶高 tool_calls
                # 代理口径、误判收敛 case 为 early_stopped)。见 src/tools/hypothesis_log.py。
                tool_call_count += sum(
                    1
                    for tc in msg.tool_calls
                    if (tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", ""))
                    != "record_hypothesis"
                )
            usage = getattr(msg, "usage_metadata", None)
            if isinstance(usage, dict) and usage.get("total_tokens"):
                real_total_tokens += int(usage["total_tokens"])
                has_real_usage = True

    if has_real_usage:
        total_tokens = real_total_tokens
    else:
        total_tokens = sum(
            estimate_tokens(str(m.content)) for m in messages if hasattr(m, "content")
        )

    now = datetime.now(UTC)
    elapsed = (now - budget.started_at).total_seconds() if budget.started_at else 0.0

    return BudgetState(
        total_tokens=budget.total_tokens + total_tokens,
        total_cost_usd=budget.total_cost_usd,
        tool_calls=budget.tool_calls + tool_call_count,
        started_at=budget.started_at or now,
        elapsed_seconds=elapsed,
        last_checked_at=now,
    )


def is_budget_exceeded(budget: BudgetState) -> bool:
    """Check if the diagnosis budget has been exceeded (model_calls / tokens / time).

    Post-hoc re-derivation (OR'd with the runtime ``budget_exhausted`` flag in the
    node). ``tool_calls`` is real tool invocations (≤ model_calls, **excluding
    §7.2 record_hypothesis 埋点**); comparing against ``MAX_MODEL_CALLS`` is a
    conservative proxy -- if tool_calls hit the model-call cap, model_calls
    certainly did too. The authoritative runtime gate is BudgetGuardMiddleware
    (counts model_call_count directly, also exempting record_hypothesis turns).
    """
    if budget.tool_calls >= MAX_MODEL_CALLS:
        return True
    return budget.total_tokens >= MAX_TOKENS_BUDGET or budget.elapsed_seconds >= MAX_TIME_SECONDS
