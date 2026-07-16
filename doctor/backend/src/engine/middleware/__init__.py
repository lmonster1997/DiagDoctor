"""Middleware pipeline sub-package.

Re-exports the 6 create_agent middlewares + the per-run ContextVar helpers so
callers can ``from src.engine.middleware import BudgetGuardMiddleware, ...``
without knowing each class's module path.

Middleware registration order (see ``engine/agent.py: build_diagnosis_agent``):
    AgentLifecycle -> ToolDedup -> LangfuseTracing
        -> ToolTruncation -> BudgetGuard -> ForcedFinalCall
"""

from __future__ import annotations

from src.engine.budget.guard import BudgetGuardMiddleware
from src.engine.middleware.forced_call import ForcedFinalCallMiddleware
from src.engine.middleware.langfuse_tracing import LangfuseTracingMiddleware
from src.engine.middleware.lifecycle import AgentLifecycleMiddleware
from src.engine.middleware.tool_dedup import ToolDedupMiddleware
from src.engine.middleware.tool_truncation import ToolTruncationMiddleware
from src.engine.run_context import (
    DiagnosisRunContext,
    clear_run_context,
    get_run_context,
    get_run_context_or_none,
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
    "get_run_context_or_none",
    "set_run_context",
]
