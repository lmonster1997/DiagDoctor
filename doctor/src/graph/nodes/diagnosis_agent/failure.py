"""Graceful failure handler — produce a best-effort fallback report on unhandled exceptions.

This is the catch-all at the outermost try/except of ``diagnosis_agent_node``.
It returns a zero-confidence report with ``early_stopped=True`` so the graph
still produces a structurally valid state update.
"""

from __future__ import annotations

from typing import Any

from src.graph.state import DiagnosisReport, DoctorState, Finding
from src.observability.logger import get_logger

logger = get_logger(__name__)


def handle_agent_failure(state: DoctorState, error: Exception) -> dict[str, Any]:
    """
    Handle agent failures gracefully — produce a best-effort fallback report.

    Args:
        state: Current DoctorState before the failure.
        error: The exception that caused the failure.

    Returns:
        Dict with fallback report and findings for state merge.
    """
    logger.error("diagnosis_agent_failure", error=str(error), case_id=state.case_id)

    return {
        "report": DiagnosisReport(
            primary_category="",
            categories=[],
            root_cause=f"诊断 Agent 执行失败：{error}",
            confidence=0.0,
            early_stopped=True,
            notes=f"Agent 异常终止: {error}",
        ),
        "findings": [
            Finding(
                agent="diagnosis_agent",
                summary=f"Agent 执行失败：{error}",
                confidence=0.0,
            )
        ],
        "early_stopped": True,
    }
