"""Budget tracking — count tool calls + estimate tokens from agent messages.

Pure accounting: this module does NOT drive any phase decision in the
baseline harness. It only computes BudgetState deltas and checks against
the hard caps in ``constants``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import AIMessage

from src.graph.nodes.diagnosis_agent.constants import (
    MAX_TIME_SECONDS,
    MAX_TOKENS_BUDGET,
    MAX_TOOL_CALLS,
    estimate_tokens,
)
from src.graph.state import BudgetState


def update_budget(budget: BudgetState, agent_result: dict[str, Any]) -> BudgetState:
    """
    Update budget state from agent execution result.

    Counts tool calls and estimates token usage from messages.

    Args:
        budget: Current budget state.
        agent_result: Result dict from agent invocation.

    Returns:
        Updated BudgetState.
    """
    messages: list[Any] = agent_result.get("messages", [])
    tool_call_count = 0

    for msg in messages:
        if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls") and msg.tool_calls:
            tool_call_count += len(msg.tool_calls)

    # Estimate tokens using tiktoken (cl100k_base, handles Chinese/English/code accurately)
    total_tokens = sum(estimate_tokens(str(m.content)) for m in messages if hasattr(m, "content"))

    now = datetime.now(UTC)
    elapsed = (now - budget.started_at).total_seconds() if budget.started_at else 0.0

    return BudgetState(
        total_tokens=budget.total_tokens + total_tokens,
        total_cost_usd=budget.total_cost_usd,  # Updated externally by cost accountant
        tool_calls=budget.tool_calls + tool_call_count,
        started_at=budget.started_at or now,
        elapsed_seconds=elapsed,
        last_checked_at=now,
    )


def is_budget_exceeded(budget: BudgetState) -> bool:
    """
    Check if the diagnosis budget has been exceeded.

    Returns True if any of:
    - Tool calls >= MAX_TOOL_CALLS (12)
    - Estimated tokens >= MAX_TOKENS_BUDGET (100k)
    - Elapsed time >= MAX_TIME_SECONDS (300s)
    """
    if budget.tool_calls >= MAX_TOOL_CALLS:
        return True
    return budget.total_tokens >= MAX_TOKENS_BUDGET or budget.elapsed_seconds >= MAX_TIME_SECONDS
