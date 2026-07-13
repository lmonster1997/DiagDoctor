"""Shim: re-exports BudgetGuardMiddleware from src.engine.budget.guard.

This shim will be removed in Phase 7 (cleanup).
"""

from src.engine.budget.guard import BudgetGuardMiddleware

__all__ = ["BudgetGuardMiddleware"]
