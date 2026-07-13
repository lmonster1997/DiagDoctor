"""Shim: re-exports from src.engine.run_context for backward compatibility.

All new code should import directly from ``src.engine.run_context``.
This shim will be removed in Phase 7 (cleanup).
"""

from src.engine.run_context import (
    DiagnosisRunContext,
    clear_run_context,
    get_run_context,
    get_run_context_or_none,
    set_run_context,
)

__all__ = [
    "DiagnosisRunContext",
    "clear_run_context",
    "get_run_context",
    "get_run_context_or_none",
    "set_run_context",
]
