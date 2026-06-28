"""
Main LangGraph definition for DiagDoctor diagnosis pipeline (v2).

Implements the refactored graph topology (Phase 1):
    START → ingest → triage → reporter → END

Future (Phase 2+):
    START → ingest → triage → {specialist ×N fan-out} → synthesis → critic
    ├─ accept → reporter → case_store → END
    └─ retry  → triage (loop)

v2 key changes from v1:
- Added ingest node (evidence normalization) before triage
- Triage now outputs multi-label TriageOutput (not single Literal)
- Removed bug_category field (migrated to triage.primary)
- DiagnosisReport uses primary_category + categories (not bug_category)
"""

from __future__ import annotations

import uuid
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from src.graph.nodes.backend_specialist import backend_specialist_node
from src.graph.nodes.ingest import ingest_node
from src.graph.nodes.triage import route_after_triage, triage_node
from src.graph.state import DiagnosisReport, DoctorState, Finding, NormalizedEvidence
from src.observability.logger import get_logger

logger = get_logger(__name__)


async def reporter_node(state: DoctorState) -> dict[str, Any]:
    """
    Reporter node (v2): generates a diagnosis report from triage + specialist findings.

    Incorporates findings from specialist agents (backend_specialist, etc.)
    if available, falling back to category-based generic report otherwise.
    """
    triage = state.triage
    findings: list[Finding] = state.findings
    primary = triage.primary or "backend_error"
    categories = [s.category for s in triage.scores] if triage.scores else [primary]
    triage_confidence = triage.scores[0].confidence if triage.scores else 0.5

    # Determine tiers from normalized evidence signals
    evidence: NormalizedEvidence = state.evidence
    has_frontend_signal = any(s.service_tier == "frontend" for s in evidence.golden_signals)
    has_backend_signal = any(s.service_tier == "backend" for s in evidence.golden_signals)
    symptom_tier: str = "frontend" if has_frontend_signal else "backend"
    root_cause_tier: str = "backend" if has_backend_signal else "frontend"

    # ── Use specialist findings if available ──────────────────────
    # Prefer specialist findings (non-TriageAgent) with actual file/code info
    specialist_findings = [
        f for f in findings if f.agent not in ("", "TriageAgent") and f.confidence > 0.0
    ]
    triage_findings = [f for f in findings if f.agent == "TriageAgent" and f.confidence > 0.0]

    # DEBUG: log all findings for inspection
    logger.info(
        "reporter_findings_summary",
        total=len(findings),
        specialist_count=len(specialist_findings),
        triage_count=len(triage_findings),
        finding_agents=[f.agent for f in findings],
        finding_confidences=[f.confidence for f in findings],
        finding_files=[f.affected_files for f in findings],
    )

    if specialist_findings:
        # Pick the specialist finding with the most complete info
        best = max(
            specialist_findings,
            key=lambda f: (
                len(f.affected_files) > 0,  # prefer findings with file info
                len(f.evidence_refs) > 0,  # prefer findings with evidence refs
                f.confidence,  # then by confidence
            ),
        )
        report = DiagnosisReport(
            primary_category=primary,
            categories=categories,
            symptom_tier=symptom_tier,  # type: ignore[arg-type]
            root_cause_tier=root_cause_tier,  # type: ignore[arg-type]
            root_cause=best.summary or f"初步分析：此问题属于 {primary} 类型。",
            affected_file=best.affected_files[0] if best.affected_files else None,
            fix_suggestion=best.fix_suggestion or "建议检查相关日志和 Trace 以定位具体根因。",
            evidence_chain=best.evidence_refs if best.evidence_refs else ["TriageAgent 多标签分类"],
            confidence=best.confidence,
            early_stopped=False,
        )
        return {"report": report}

    # Use triage finding if available
    if triage_findings:
        best_triage = max(triage_findings, key=lambda f: f.confidence)
        report = DiagnosisReport(
            primary_category=primary,
            categories=categories,
            symptom_tier=symptom_tier,  # type: ignore[arg-type]
            root_cause_tier=root_cause_tier,  # type: ignore[arg-type]
            root_cause=best_triage.summary or f"初步分析：此问题属于 {primary} 类型。",
            affected_file=best_triage.affected_files[0] if best_triage.affected_files else None,
            fix_suggestion=best_triage.fix_suggestion
            or "建议检查相关日志和 Trace 以定位具体根因。",
            evidence_chain=best_triage.evidence_refs
            if best_triage.evidence_refs
            else ["TriageAgent 多标签分类"],
            confidence=best_triage.confidence,
            early_stopped=False,
        )
        return {"report": report}

    # ── Fallback: basic category-based report ────────────────────
    category_descriptions: dict[str, str] = {
        "frontend_crash": "前端运行时崩溃，可能与 JS 错误、组件渲染异常或未捕获的 Promise 相关。",
        "backend_error": "后端服务异常，可能与未处理异常、数据库错误或外部服务调用失败相关。",
        "performance": "性能问题，可能与 N+1 查询、缓存缺失或慢 API 调用相关。",
        "logic": "业务逻辑错误，可能与权限校验、数据状态管理或流程控制相关。",
        "data": "数据问题，可能与编码、精度丢失或时区处理相关。",
        "config": "配置或环境问题，可能与 CORS、环境变量或服务发现相关。",
    }

    description = category_descriptions.get(primary, "需要进一步排查。")

    report = DiagnosisReport(
        primary_category=primary,
        categories=categories,
        symptom_tier=symptom_tier,  # type: ignore[arg-type]
        root_cause_tier=root_cause_tier,  # type: ignore[arg-type]
        root_cause=f"初步分析：此问题属于 {primary} 类型。{description}",
        fix_suggestion="建议检查相关日志和 Trace 以定位具体根因。",
        evidence_chain=["TriageAgent 多标签分类"],
        confidence=triage_confidence,
        early_stopped=False,
    )

    return {"report": report}


# ── Graph construction ──────────────────────────────────────────────


_graph_instance: Any = None


def _get_checkpointer() -> MemorySaver:
    """Create a MemorySaver checkpointer for development."""
    return MemorySaver()


def build_graph() -> Any:
    """
    Build (but not compile) the DiagDoctor diagnosis graph (v2).

    Phase 2 topology (specialist fan-out):
        START → ingest → triage → backend_specialist → reporter → END

    The conditional edge routes from triage based on confidence gating.
    For now, backend_error cases fan out to backend_specialist.
    Other categories go directly to reporter.
    """
    graph: StateGraph[DoctorState, None, DoctorState, DoctorState] = StateGraph(DoctorState)

    # ── Nodes ──
    graph.add_node("ingest", ingest_node)
    graph.add_node("triage", triage_node)
    graph.add_node("backend_specialist", backend_specialist_node)
    graph.add_node("reporter", reporter_node)

    # ── Edges ──
    graph.set_entry_point("ingest")
    graph.add_edge("ingest", "triage")

    # Conditional fan-out from triage: route to specialist or skip to reporter
    graph.add_conditional_edges(
        "triage",
        route_after_triage,
        {
            "backend_specialist": "backend_specialist",
            "frontend_specialist": "reporter",  # not yet implemented → skip to reporter
            "perf_specialist": "reporter",  # not yet implemented → skip to reporter
            "logic_specialist": "reporter",  # not yet implemented → skip to reporter
            "general_agent": "reporter",  # low-confidence fallback → skip to reporter
        },
    )
    graph.add_edge("backend_specialist", "reporter")
    graph.add_edge("reporter", END)

    return graph


def get_graph() -> Any:
    """
    Get or create the compiled DiagDoctor graph with MemorySaver checkpointer.

    The graph is cached at module level for reuse across requests.
    Uses SqliteSaver so diagnosis sessions can be resumed via thread_id.
    """
    global _graph_instance
    if _graph_instance is None:
        checkpointer = _get_checkpointer()
        _graph_instance = build_graph().compile(checkpointer=checkpointer)
    return _graph_instance


def generate_thread_id() -> str:
    """Generate a unique thread_id for a new diagnosis session."""
    return f"diag-{uuid.uuid4().hex[:12]}"
