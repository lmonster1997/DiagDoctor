"""Shim: re-exports all state types from src.engine.state for backward compatibility.

All new code should import directly from ``src.engine.state``.
This shim will be removed in Phase 7 (cleanup).
"""

from src.engine.state import (
    BudgetState,
    BugCategory,
    Correlation,
    DiagnosisHypothesis,
    DiagnosisReport,
    DoctorState,
    Evidence,
    Finding,
    LogEntry,
    NormalizedEvidence,
    Signal,
    TraceSpan,
    VALID_CATEGORIES,
)

__all__ = [
    "BudgetState",
    "BugCategory",
    "Correlation",
    "DiagnosisHypothesis",
    "DiagnosisReport",
    "DoctorState",
    "Evidence",
    "Finding",
    "LogEntry",
    "NormalizedEvidence",
    "Signal",
    "TraceSpan",
    "VALID_CATEGORIES",
]
