"""Middleware pipeline sub-package.

Re-exports the 7 create_agent middlewares + the ``DiagnosisRunContext`` type
(passed as ``create_agent(context_schema=...)`` and injected per-invocation via
``agent.ainvoke(..., context=run_ctx)``) so callers can
``from src.engine.middleware import BudgetGuardMiddleware, ...`` without
knowing each class's module path.

Middleware registration order (see ``engine/agent.py: build_diagnosis_agent``):
    AgentLifecycle -> ToolDedup -> LangfuseTracing
        -> ToolTruncation -> ContextElision -> BudgetGuard -> ForcedFinalCall
"""

from __future__ import annotations

from src.engine.budget.guard import BudgetGuardMiddleware
from src.engine.middleware.context_elision import ContextElisionMiddleware
from src.engine.middleware.forced_call import ForcedFinalCallMiddleware
from src.engine.middleware.langfuse_tracing import LangfuseTracingMiddleware
from src.engine.middleware.lifecycle import AgentLifecycleMiddleware
from src.engine.middleware.tool_dedup import ToolDedupMiddleware
from src.engine.middleware.tool_truncation import ToolTruncationMiddleware
from src.engine.run_context import DiagnosisRunContext

__all__ = [
    "AgentLifecycleMiddleware",
    "BudgetGuardMiddleware",
    "ContextElisionMiddleware",
    "DiagnosisRunContext",
    "ForcedFinalCallMiddleware",
    "LangfuseTracingMiddleware",
    "ToolDedupMiddleware",
    "ToolTruncationMiddleware",
]
