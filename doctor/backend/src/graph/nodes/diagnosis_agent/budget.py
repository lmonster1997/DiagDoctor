"""Shim: re-exports from src.engine.budget for backward compatibility.

This shim will be removed in Phase 7 (cleanup).
"""

from src.engine.budget.constants import (
    BUDGET_WARNING_THRESHOLD,
    MAX_TIME_SECONDS,
    MAX_TOKENS_BUDGET,
    MAX_TOOL_CALLS,
)
from src.engine.budget.tracker import estimate_tokens, is_budget_exceeded, update_budget

__all__ = [
    "BUDGET_WARNING_THRESHOLD",
    "MAX_TIME_SECONDS",
    "MAX_TOKENS_BUDGET",
    "MAX_TOOL_CALLS",
    "estimate_tokens",
    "is_budget_exceeded",
    "update_budget",
]
