"""Shim: re-exports from src.engine.context.* for backward compatibility.

All new code should import directly from ``src.engine.context``.
This shim will be removed in Phase 7 (cleanup).
"""

from src.engine.context.budget import ContextBudget, ContextPhase, estimate_tokens
from src.engine.context.truncation import TOOL_CHAR_LIMITS, truncate_tool_result
from src.engine.context.compaction import degrade_old_tool_results, maybe_compact_context
from src.engine.context.dynamic_prompt import build_dynamic_system_prompt

__all__ = [
    "ContextBudget",
    "ContextPhase",
    "TOOL_CHAR_LIMITS",
    "build_dynamic_system_prompt",
    "degrade_old_tool_results",
    "estimate_tokens",
    "maybe_compact_context",
    "truncate_tool_result",
]
