"""
Backend Specialist LangGraph node — wraps the ReAct agent as a node.

Connects the Backend Specialist subgraph into the main DiagDoctor graph.
Formats the normalized evidence from DoctorState, invokes the ReAct agent,
and parses the result into a Finding.

Key design:
    - Only passes **normalized** golden_signals + correlations to the agent
    - Raw evidence (logs, traces) is NOT passed — agent uses tools on demand
    - This avoids prompt explosion while keeping the agent context-rich

Usage (in main_graph.py)::

    from src.graph.nodes.backend_specialist import backend_specialist_node

    g.add_node("backend_specialist", backend_specialist_node)

The node expects ``DoctorState`` with ``evidence`` (NormalizedEvidence) populated
by the preceding ``ingest`` node.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from langchain_core.messages import HumanMessage

from src.graph.state import (
    Correlation,
    DoctorState,
    Finding,
    NormalizedEvidence,
    RetrievalRecord,
    Signal,
)
from src.observability.logger import get_logger
from src.observability.tracing import traced

logger = get_logger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────


def _extract_evidence_time_range(
    evidence: NormalizedEvidence,
) -> tuple[str, str] | None:
    """Extract the time range covered by the evidence signals.

    Scans golden_signals for the earliest and latest timestamps to
    provide the specialist agent with a bounded query window.

    Returns:
        (start_iso, end_iso) or None if no timestamps found.
    """
    timestamps: list[datetime] = []
    for sig in evidence.golden_signals:
        ts = sig.timestamp
        if ts:
            try:
                if isinstance(ts, datetime):
                    timestamps.append(ts)
                elif isinstance(ts, str):
                    # Try ISO format
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    timestamps.append(dt)
            except (ValueError, TypeError):
                pass

    if not timestamps:
        return None

    return (
        min(timestamps).isoformat(),
        max(timestamps).isoformat(),
    )


# ── Evidence formatting ──────────────────────────────────────────────


def format_normalized_evidence(evidence: NormalizedEvidence) -> str:
    """
    Format NormalizedEvidence into a compact prompt for the Backend Specialist.

    Only includes golden_signals and correlations — raw logs/traces are
    omitted to avoid prompt explosion. The agent retrieves raw data on
    demand via tools (log_search, trace_query, etc.).

    Args:
        evidence: The NormalizedEvidence from the Ingest layer.

    Returns:
        Formatted string suitable as a HumanMessage content.
    """
    parts: list[str] = []

    # ── User report ──
    if evidence.user_report:
        parts.append(f"【用户报告】\n{evidence.user_report}\n")

    # ── Golden signals ──
    if evidence.golden_signals:
        parts.append("【黄金信号（golden_signals）】")
        parts.append(_format_signals(evidence.golden_signals))
    else:
        parts.append("【黄金信号】\n（无关键信号，请使用工具主动探查）")

    # ── Correlations ──
    if evidence.correlations:
        parts.append("\n【跨层关联（correlations）】")
        parts.append(_format_correlations(evidence.correlations))
    else:
        parts.append("\n【跨层关联】\n（无跨层关联数据）")

    # ── Context summary ──
    parts.append("\n【证据上下文】")
    parts.append(
        f"- 前端 span 数：{evidence.frontend_span_count}\n"
        f"- 后端 span 数：{evidence.backend_span_count}\n"
        f"- 噪声占比：{evidence.noise_ratio:.0%}"
    )

    # ── Evidence time range ──
    time_range = _extract_evidence_time_range(evidence)
    if time_range:
        parts.append(
            f"\n【证据时间范围】\n"
            f"- 起始：{time_range[0]}\n"
            f"- 结束：{time_range[1]}\n"
            f"（使用日志/Trace 查询工具时，请以此时段 ±2 小时为窗口，"
            f"不要使用跨越多天的时间范围。）"
        )

    # ── Instruction ──
    parts.append(
        "\n---\n"
        "请基于以上归一化证据，按你的系统提示词流程进行诊断：\n"
        "1. 分析 golden_signals 找异常类型与触发位置\n"
        "2. 用 correlations 沿 trace_id 串联请求→日志→DB 查询\n"
        "3. 用 code_search / db_query 等工具按需深挖\n"
        "4. 最终输出一个 JSON 格式的 Finding（含 summary/affected_files/"
        "fix_suggestion/evidence_refs/confidence/contradiction/cross_layer）\n\n"
        "注意：原始日志和 trace 已索引在 raw_refs 中，你可以用 "
        "query_loki_logs / query_tempo_trace 按需获取原文。"
    )

    return "\n".join(parts)


def _format_signals(signals: list[Signal]) -> str:
    """Format golden signals compactly."""
    lines: list[str] = []
    for sig in signals[:30]:  # cap at 30 to avoid overloading prompt
        tier_label = "前端" if sig.service_tier == "frontend" else "后端"
        sev_label = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}.get(sig.severity, "•")
        ref = f" [{sig.signal_id}]" if sig.signal_id else ""
        lines.append(
            f"  {sev_label} [{tier_label}] [{sig.source}/{sig.signal_type}] {sig.summary}{ref}"
        )
    if not lines:
        return "  （无信号）"
    return "\n".join(lines)


def _format_correlations(correlations: list[Correlation]) -> str:
    """Format cross-layer correlations compactly."""
    lines: list[str] = []
    for corr in correlations[:10]:
        trace_str = f" trace={corr.trace_id}" if corr.trace_id else ""
        lines.append(
            f"  - [{corr.correlation_id}]{trace_str} confidence={corr.confidence:.1f}: "
            f"{corr.description}"
        )
    if not lines:
        return "  （无关联）"
    return "\n".join(lines)


# ── Node function ────────────────────────────────────────────────────


@traced()
async def backend_specialist_node(state: DoctorState) -> dict[str, Any]:
    """
    LangGraph node: analyze backend evidence → produce a Finding.

    Uses the ReAct agent (``get_backend_specialist()``) with shared tools
    (code_search, log_search, trace_query, db_query) to perform deep-dive
    diagnosis. The agent can call tools iteratively to locate root cause
    at the code level.

    Args:
        state: Current DoctorState (after Ingest + Triage).

    Returns:
        Dict with 'findings' key for state merge.
    """
    from src.graph.subgraphs.backend_specialist import (
        get_backend_specialist,
        parse_agent_output_to_finding,
    )

    evidence: NormalizedEvidence = state.evidence

    # Skip if no meaningful evidence
    if not evidence.golden_signals and not evidence.correlations:
        logger.warning("backend_specialist_skipped_no_evidence")
        return {
            "findings": [
                Finding(
                    agent="backend_specialist",
                    summary="证据不足，跳过后端诊断",
                    confidence=0.0,
                )
            ]
        }

    user_message = format_normalized_evidence(evidence)

    logger.info(
        "backend_specialist_invoking",
        signal_count=len(evidence.golden_signals),
        correlation_count=len(evidence.correlations),
    )

    # Phase 2: ReAct agent with tool access
    try:
        agent = get_backend_specialist()
        agent_result = await agent.ainvoke({"messages": [HumanMessage(content=user_message)]})
        finding = parse_agent_output_to_finding(agent_result)
    except Exception as exc:
        logger.error("backend_specialist_agent_error", error=str(exc))
        return {
            "findings": [
                Finding(
                    agent="backend_specialist",
                    summary=f"Agent 执行失败：{exc}",
                    confidence=0.0,
                )
            ]
        }

    logger.info(
        "backend_specialist_completed",
        summary=finding.summary[:200] if finding.summary else "",
        confidence=finding.confidence,
        evidence_refs_count=len(finding.evidence_refs),
        affected_files_count=len(finding.affected_files),
    )

    return {"findings": [finding]}


def _extract_retrieval_trace(agent_result: dict[str, Any]) -> list[RetrievalRecord]:
    """Extract retrieval trace from agent result for evaluation (stub)."""
    return []
