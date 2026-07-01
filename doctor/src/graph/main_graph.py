"""
Main LangGraph definition for DiagDoctor diagnosis pipeline (v3).

V3 topology (3 nodes, linear):
    START → ingest → unified_agent → reporter → END

    ingest:        Collects logs+traces from Loki/Tempo for backend + frontend
                   (parallel fetch), then runs deterministic normalization pipeline
                   (denoise→dedup→tree→signals→correlate→index).
    unified_agent: Formats normalized evidence → ReAct LLM loop with 5 tools
                   (search_observability, code_search, db_query,
                    inspect_frontend_error, get_file_content).
    reporter:      Best-effort fallback when unified_agent output is invalid.

No conditional routing, no specialist fan-out, no critic loop.
"""

from __future__ import annotations

import uuid
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from src.graph.nodes.ingest import ingest_node
from src.graph.nodes.unified_agent import unified_agent_node
from src.graph.state import DiagnosisReport, DoctorState, Finding, NormalizedEvidence
from src.observability.logger import get_logger

logger = get_logger(__name__)


# ── Best-effort fallback ────────────────────────────────────────────


def _best_effort_report(state: DoctorState) -> DiagnosisReport:
    """
    Construct a best-effort diagnosis report when the unified_agent fails.

    Uses available findings and evidence signals to produce a skeleton
    report with minimal information.
    """
    findings: list[Finding] = state.findings
    evidence: NormalizedEvidence = state.evidence

    # Determine tiers from evidence signals
    has_frontend = any(s.service_tier == "frontend" for s in evidence.golden_signals)
    has_backend = any(s.service_tier == "backend" for s in evidence.golden_signals)
    symptom_tier = "frontend" if has_frontend else "backend"
    root_cause_tier = "backend" if has_backend else "frontend"

    # Extract best info from findings
    if findings:
        best = max(findings, key=lambda f: f.confidence)
        return DiagnosisReport(
            primary_category="",
            categories=[],
            symptom_tier=symptom_tier,  # type: ignore[arg-type]
            root_cause_tier=root_cause_tier,  # type: ignore[arg-type]
            root_cause=best.summary or "诊断未完成",
            affected_file=best.affected_files[0] if best.affected_files else None,
            fix_suggestion=best.fix_suggestion or "",
            evidence_chain=best.evidence_refs,
            confidence=best.confidence,
            early_stopped=True,
            notes="Agent 未产出完整报告，使用 best-effort 兜底",
        )

    return DiagnosisReport(
        root_cause="证据不足，无法完成诊断",
        confidence=0.0,
        early_stopped=True,
        notes="无 findings 且 Agent 未产出报告",
    )


# ── Reporter node (v3 simplified) ───────────────────────────────────


async def reporter_node(state: DoctorState) -> dict[str, Any]:
    """
    Reporter node (v3): simplified — unified_agent directly produces DiagnosisReport.

    In V3, the unified_agent already outputs a structured DiagnosisReport
    (with primary_category, categories, root_cause, fix_suggestion, etc.).
    The reporter's only job is to provide a best-effort fallback when the
    agent's report is None (e.g. budget exceeded, agent error).
    """
    report = state.report

    if report is None:
        logger.warning("reporter_fallback", case_id=state.case_id)
        report = _best_effort_report(state)
    else:
        logger.info(
            "reporter_passthrough",
            case_id=state.case_id,
            primary_category=report.primary_category,
            confidence=report.confidence,
        )

    return {"report": report}


# ── Graph construction ──────────────────────────────────────────────


_graph_instance: Any = None


def _get_checkpointer() -> MemorySaver:
    """Create a MemorySaver checkpointer for development."""
    return MemorySaver()


def build_graph() -> Any:
    """
    Build the DiagDoctor diagnosis graph (v3).

    V3 linear topology (3 nodes, 2 edges):
        START → ingest → unified_agent → reporter → END

    No conditional routing, no specialist fan-out, no critic loop.
    The unified_agent performs triage internally via its System Prompt.
    """
    graph: StateGraph[DoctorState, None, DoctorState, DoctorState] = StateGraph(DoctorState)

    # ── 3 nodes ──
    graph.add_node("ingest", ingest_node)
    graph.add_node("unified_agent", unified_agent_node)
    graph.add_node("reporter", reporter_node)

    # ── Linear edges ──
    graph.set_entry_point("ingest")
    graph.add_edge("ingest", "unified_agent")
    graph.add_edge("unified_agent", "reporter")
    graph.add_edge("reporter", END)

    return graph


def get_graph() -> Any:
    """
    Get or create the compiled DiagDoctor graph with MemorySaver checkpointer.

    The graph is cached at module level for reuse across requests.
    """
    global _graph_instance
    if _graph_instance is None:
        checkpointer = _get_checkpointer()
        _graph_instance = build_graph().compile(checkpointer=checkpointer)
    return _graph_instance


def generate_thread_id() -> str:
    """Generate a unique thread_id for a new diagnosis session."""
    return f"diag-{uuid.uuid4().hex[:12]}"
