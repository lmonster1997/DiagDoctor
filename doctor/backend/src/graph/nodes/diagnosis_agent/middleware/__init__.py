"""Shim: re-exports from src.engine.middleware for backward compatibility.

This shim will be removed in Phase 7 (cleanup).
"""

from src.engine.middleware.lifecycle import AgentLifecycleMiddleware
from src.engine.middleware.tool_dedup import ToolDedupMiddleware
from src.engine.middleware.tool_truncation import ToolTruncationMiddleware
from src.engine.middleware.langfuse_tracing import LangfuseTracingMiddleware
from src.engine.budget.guard import BudgetGuardMiddleware
from src.engine.middleware.forced_call import ForcedFinalCallMiddleware
from src.engine.run_context import (
    DiagnosisRunContext,
    clear_run_context,
    get_run_context,
    set_run_context,
)

__all__ = [
    "AgentLifecycleMiddleware",
    "BudgetGuardMiddleware",
    "DiagnosisRunContext",
    "ForcedFinalCallMiddleware",
    "LangfuseTracingMiddleware",
    "ToolDedupMiddleware",
    "ToolTruncationMiddleware",
    "clear_run_context",
    "get_run_context",
    "set_run_context",
]
