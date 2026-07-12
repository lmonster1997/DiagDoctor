"""Langfuse trace finalization — shared by CopilotKit and REST paths."""

from __future__ import annotations

from typing import Any

from src.graph.state import DiagnosisReport
from src.observability.logger import get_logger

logger = get_logger(__name__)


def _finalize_langfuse_trace(
    langfuse_handler: Any | None,
    report: DiagnosisReport,
    early_stopped: bool,
    budget_state: Any,
    forced_call_triggered: bool,
    case_id: str,
) -> None:
    """End the Langfuse trace with the report + run flags. Errors are non-fatal."""
    if langfuse_handler is None:
        return
    try:
        report_dict = (
            report.model_dump(mode="json") if hasattr(report, "model_dump") else {}
        )
        langfuse_handler.end_trace(
            output_data={
                "diagnosis_report": report_dict,
                "early_stopped": early_stopped,
                "tool_calls": budget_state.tool_calls,
                "forced_final_json_call": forced_call_triggered,
            },
        )
        logger.debug(
            "langfuse_trace_finalized",
            case_id=case_id,
            primary_category=report.primary_category,
            confidence=report.confidence,
            early_stopped=early_stopped,
        )
    except Exception as lf_exc:
        logger.debug(
            "langfuse_end_trace_error",
            case_id=case_id,
            error=str(lf_exc),
        )
