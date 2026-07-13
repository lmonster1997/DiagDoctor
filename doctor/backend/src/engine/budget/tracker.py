"""预算状态追踪 — update_budget + is_budget_exceeded。

用于 CopilotKit 路径中的诊断节点后处理。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import tiktoken
from langchain_core.messages import AIMessage

from src.engine.budget.constants import MAX_TIME_SECONDS, MAX_TOKENS_BUDGET, MAX_TOOL_CALLS
from src.engine.state import BudgetState

_encoder = tiktoken.get_encoding("cl100k_base")


def estimate_tokens(text: str) -> int:
    """Estimate token count using cl100k_base (OpenAI-compatible models)."""
    return len(_encoder.encode(text))


def update_budget(budget: BudgetState, agent_result: dict[str, Any]) -> BudgetState:
    """Update budget state from agent execution result.

    Counts tool calls and estimates token usage from messages.
    """
    messages: list[Any] = agent_result.get("messages", [])
    tool_call_count = 0

    for msg in messages:
        if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls") and msg.tool_calls:
            tool_call_count += len(msg.tool_calls)

    total_tokens = sum(estimate_tokens(str(m.content)) for m in messages if hasattr(m, "content"))

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
    """Check if the diagnosis budget has been exceeded (tool_calls / tokens / time)."""
    if budget.tool_calls >= MAX_TOOL_CALLS:
        return True
    return budget.total_tokens >= MAX_TOKENS_BUDGET or budget.elapsed_seconds >= MAX_TIME_SECONDS
