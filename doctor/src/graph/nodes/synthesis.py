"""
Synthesis node — fan-in aggregation of specialist findings.

Aggregates findings from one or more specialist agents (potentially run in
parallel via LangGraph fan-out) into a unified ``draft_report``. Handles:

1. **Merging**: combines findings from multiple specialists
2. **Conflict resolution**: when two specialists disagree, picks the higher-confidence
   finding or marks the report as uncertain
3. **Evidence chaining**: constructs a unified ``evidence_chain`` from all findings
4. **Confidence aggregation**: computes an aggregate confidence score

The draft_report is then passed to:
- ``reporter`` (for final formatting, Phase 2)
- ``critic`` (for validation loop, Phase 3 / D29)

Per the handbook (D25): "合并多个 specialist 的 findings，去冲突、按 confidence 聚合，
生成统一的 draft_report"
"""

from __future__ import annotations

from typing import Any

from src.graph.state import DiagnosisReport, DoctorState, Finding, NormalizedEvidence
from src.observability.logger import get_logger
from src.observability.tracing import traced

logger = get_logger(__name__)


@traced()
async def synthesis_node(state: DoctorState) -> dict[str, Any]:
    """
    Synthesis node: aggregate specialist findings into a draft report.

    This is the fan-in point where all parallel specialist branches converge.
    It produces a ``draft_report`` (DiagnosisReport) that the reporter (and
    later the critic) will consume.

    Aggregation rules:
    - Primary category and tier info come from triage (authoritative)
    - Root cause and fix suggestion come from the highest-confidence finding
    - Affected files are aggregated from ALL findings (deduplicated)
    - Evidence chain merges all evidence_refs across specialists
    - If no specialist findings exist, falls back to triage-only skeleton

    Args:
        state: Current DoctorState with triage + accumulated findings.

    Returns:
        Dict with ``draft_report`` key for state merge.
    """
    triage = state.triage
    primary = triage.primary or "backend_error"
    categories = [s.category for s in triage.scores] if triage.scores else [primary]

    # ── Tier determination ──
    evidence: NormalizedEvidence = state.evidence
    has_frontend = any(s.service_tier == "frontend" for s in evidence.golden_signals)
    has_backend = any(s.service_tier == "backend" for s in evidence.golden_signals)
    symptom_tier: str = "frontend" if has_frontend else "backend"
    root_cause_tier: str = "backend" if has_backend else "frontend"

    # ── Filter specialist findings ──
    all_findings: list[Finding] = state.findings
    specialist_findings = [f for f in all_findings if f.agent != "TriageAgent"]
    triage_findings = [f for f in all_findings if f.agent == "TriageAgent"]

    # ── Defaults (triage-only fallback) ──
    category_descriptions: dict[str, str] = {
        "frontend_crash": "前端运行时崩溃",
        "backend_error": "后端服务异常",
        "performance": "性能问题",
        "logic": "业务逻辑错误",
        "data": "数据问题",
        "config": "配置或环境问题",
    }
    description = category_descriptions.get(primary, "需要进一步排查")

    root_cause = f"初步分析：此问题属于 {primary} 类型。{description}"
    fix_suggestion = "建议检查相关日志和 Trace 以定位具体根因。"
    evidence_chain: list[str] = []
    confidence = triage.scores[0].confidence if triage.scores else 0.5
    affected_files: list[str] = []
    cross_layer = triage.cross_layer_suspected
    early_stopped = False

    # ── Incorporate specialist findings ──
    if specialist_findings:
        logger.info(
            "synthesis_aggregating",
            specialist_count=len(specialist_findings),
            finding_count=len(all_findings),
        )

        # Sort by confidence descending
        sorted_findings = sorted(specialist_findings, key=lambda f: -f.confidence)

        # Use the highest-confidence finding as primary source
        best = sorted_findings[0]

        if best.summary:
            root_cause = best.summary
        if best.fix_suggestion:
            fix_suggestion = best.fix_suggestion
        confidence = max(confidence, best.confidence)

        # Detect contradictions between specialists
        contradictions = _detect_contradictions(sorted_findings)
        if contradictions:
            logger.warning("synthesis_contradictions_detected", contradictions=contradictions)
            root_cause = (
                f"[多专家结论不一致] {root_cause}\n"
                f"注意：其他专家提出不同观点：{'；'.join(contradictions)}"
            )

        # Aggregate evidence_refs from ALL findings (deduplicated)
        all_refs: list[str] = []
        for f in sorted_findings:
            all_refs.extend(f.evidence_refs)
        evidence_chain = list(dict.fromkeys(all_refs))  # dedup preserving order

        # Aggregate affected_files from ALL findings (deduplicated)
        all_files: list[str] = []
        for f in sorted_findings:
            all_files.extend(f.affected_files)
        affected_files = list(dict.fromkeys(all_files))

        # Cross-layer: if any specialist flagged cross_layer, propagate
        if any(f.cross_layer for f in sorted_findings):
            cross_layer = True
            # If cross-layer, the root_cause_tier should match the actual root cause
            # (a frontend symptom with backend root cause → root_cause_tier = backend)
            if symptom_tier == "frontend" and any(
                f.affected_files
                and any("backend" in af.lower() or af.endswith(".py") for af in f.affected_files)
                for f in sorted_findings
            ):
                root_cause_tier = "backend"

        # Check if any finding has contradiction=True (evidence contradicts triage category)
        if any(f.contradiction for f in sorted_findings):
            root_cause = (
                f"[证据与分类存疑] {root_cause}\n"
                f"注意：部分发现表明证据与 triage 分类（{primary}）存在矛盾，"
                f"建议 Critic 触发 re-triage。"
            )

    # Also collect evidence from triage findings
    for tf in triage_findings:
        if tf.evidence_refs:
            for ref in tf.evidence_refs:
                if ref not in evidence_chain:
                    evidence_chain.append(ref)

    # Prepend triage to evidence chain
    if "TriageAgent 多标签分类" not in evidence_chain:
        evidence_chain.insert(0, "TriageAgent 多标签分类")

    # ── Build draft report ──
    draft_report = DiagnosisReport(
        primary_category=primary,
        categories=categories,
        symptom_tier=symptom_tier,  # type: ignore[arg-type]
        root_cause_tier=root_cause_tier,  # type: ignore[arg-type]
        root_cause=root_cause,
        affected_file=affected_files[0] if affected_files else None,
        fix_suggestion=fix_suggestion,
        evidence_chain=evidence_chain,
        confidence=confidence,
        early_stopped=early_stopped,
    )

    logger.info(
        "synthesis_complete",
        primary=primary,
        finding_count=len(specialist_findings),
        confidence=confidence,
        evidence_refs=len(evidence_chain),
        affected_files=len(affected_files),
        cross_layer=cross_layer,
    )

    return {"draft_report": draft_report}


def _detect_contradictions(findings: list[Finding]) -> list[str]:
    """
    Detect contradictory conclusions between specialist findings.

    Two findings are considered contradictory if:
    - They point to different files AND both have confidence > 0.6
    - The lower-confidence one is not marked as cross_layer (which
      would indicate it's pointing to a different tier, not disagreeing)

    Args:
        findings: Specialist findings sorted by confidence descending.

    Returns:
        List of contradiction descriptions (empty if consistent).
    """
    if len(findings) < 2:
        return []

    contradictions: list[str] = []
    primary = findings[0]

    for other in findings[1:]:
        # Skip low-confidence or cross_layer findings (they complement, not contradict)
        if other.confidence < 0.5 or other.cross_layer:
            continue

        # Check if they point to fundamentally different files
        primary_files = set(primary.affected_files)
        other_files = set(other.affected_files)
        if primary_files and other_files and not primary_files & other_files:
            contradictions.append(
                f"{other.agent}(conf={other.confidence:.1f}) → {other.summary[:100]}"
            )

    return contradictions
