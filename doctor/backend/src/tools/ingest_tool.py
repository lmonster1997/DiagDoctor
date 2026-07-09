"""
Ingest Tool — 将 ingest 管线暴露为 DiagnosisAgent 可调用的 LangChain Tool。

当用户在 CopilotKit 聊天中描述 Bug 时，Agent 调用此工具：
1. 从 Loki/Tempo 采集日志和 Trace（可选按 trace_id / trigger_time 精确查询）
2. 运行标准化管线（denoise→dedup→signals→correlate）
3. 返回结构化证据摘要，供 Agent 后续诊断使用

Usage::

    from src.tools.ingest_tool import INGEST_TOOL

    # Agent 调用:
    result = await run_ingest(
        user_report="点击创建任务按钮后页面报 500 错误",
        trace_id="abc123...",
    )
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from langchain_core.tools import StructuredTool

from src.config import settings
from src.observability.logger import get_logger
from src.tools.observability_unified import search_observability

logger = get_logger(__name__)


def _format_evidence_summary(evidence: Any, user_report: str) -> str:
    """将 NormalizedEvidence 格式化为 Agent 友好的文本摘要。"""
    signals = getattr(evidence, "golden_signals", [])
    correlations = getattr(evidence, "correlations", [])
    timeline = getattr(evidence, "timeline", [])
    raw_refs = getattr(evidence, "raw_refs", {})

    parts: list[str] = []

    parts.append(f"## 用户报告\n{user_report}\n")

    # Golden signals
    if signals:
        parts.append(f"## 关键信号 ({len(signals)} 条)")
        for s in signals:
            tier = getattr(s, "service_tier", "unknown")
            sig_type = getattr(s, "signal_type", "unknown")
            severity = getattr(s, "severity", "info")
            summary = getattr(s, "summary", "")
            parts.append(f"- [{tier}][{severity}][{sig_type}] {summary}")
    else:
        parts.append("## 关键信号\n(未检测到明显异常信号)")

    # Correlations
    if correlations:
        parts.append(f"\n## 跨层关联 ({len(correlations)} 条)")
        for c in correlations:
            c_type = getattr(c, "correlation_type", "unknown")
            c_desc = getattr(c, "description", "")
            parts.append(f"- [{c_type}] {c_desc}")

    # Timeline summary
    if timeline:
        error_count = sum(1 for e in timeline if getattr(e, "source", "") == "browser_error")
        log_count = sum(1 for e in timeline if getattr(e, "source", "") == "log")
        trace_count = sum(1 for e in timeline if getattr(e, "source", "") == "trace")
        parts.append(
            f"\n## 时间线摘要\n"
            f"日志 {log_count} 条 / Trace span {trace_count} 条 / 前端错误 {error_count} 条"
        )

    # Raw refs index
    if raw_refs:
        total_items = sum(len(v) if isinstance(v, list) else 0 for v in raw_refs.values())
        parts.append(f"\n## 数据索引\n共 {total_items} 条原始记录可供深度查询")

    return "\n".join(parts)


async def run_ingest(
    user_report: str,
    trace_id: str = "",
    trigger_time: str = "",
) -> str:
    """采集可观测性数据并标准化，返回证据摘要供后续诊断。

    优先使用 trace_id 精确查询；其次用 trigger_time 时间窗口查询；
    两者都为空时仅对 user_report 做基础标准化。

    Args:
        user_report: 用户对 Bug 的完整描述（错误现象、操作步骤等）。
        trace_id: （可选）32 位 hex Trace ID，用于精确查询关联日志和 Trace。
        trigger_time: （可选）ISO 格式时间戳，如 '2026-07-09T10:30:00'，
                      用于限定 ±5 分钟查询窗口。

    Returns:
        JSON 格式的证据摘要，包含关键信号、跨层关联、时间线等。
    """
    raw_logs: list[dict[str, Any]] = []
    raw_traces: list[dict[str, Any]] = []
    browser_errors: list[dict[str, Any]] = []

    # Resolve time window
    if trigger_time:
        try:
            tt = datetime.fromisoformat(trigger_time)
        except ValueError:
            tt = datetime.utcnow()
    else:
        tt = datetime.utcnow()

    start = (tt - timedelta(minutes=5)).isoformat()
    end = (tt + timedelta(minutes=5)).isoformat()

    # ── Phase 1: Collect observability data ──
    if trace_id:
        # Precise: query by trace_id
        logger.info("ingest_tool_precise_query", trace_id=trace_id)
        try:
            # Tempo: full trace
            tempo_result = await search_observability(
                source="tempo", query=trace_id, analysis="full", start=start, end=end
            )
            tdata = json.loads(tempo_result)
            raw_traces = tdata.get("traces", [])
            browser_errors = tdata.get("analysis", {}).get("error_spans", [])
        except Exception as exc:
            logger.warning("ingest_tool_tempo_failed", trace_id=trace_id, error=str(exc))

        try:
            # Loki: logs for this trace_id (backend + frontend)
            logql = (
                '{service_name=~"demo-backend|demo-frontend",'
                ' trace_id=~"' + trace_id + '"}'
            )
            loki_result = await search_observability(
                source="loki", query=logql, start=start, end=end, limit=200
            )
            ldata = json.loads(loki_result)
            raw_logs = ldata.get("logs", [])
        except Exception as exc:
            logger.warning("ingest_tool_loki_failed", trace_id=trace_id, error=str(exc))

    elif trigger_time:
        # Time-window: query by service + time range
        logger.info("ingest_tool_time_query", trigger_time=trigger_time)
        try:
            result = await search_observability(
                source="auto",
                query='{service_name=~"demo-backend|demo-frontend"}',
                start=start,
                end=end,
                analysis="errors",
                limit=100,
                include_frontend=True,
            )
            data = json.loads(result)
            raw_logs = data.get("logs", [])
            raw_traces = data.get("traces", [])
            browser_errors = data.get("frontend_errors", [])
        except Exception as exc:
            logger.warning("ingest_tool_time_query_failed", error=str(exc))

    # ── Phase 2: Normalize ──
    raw_evidence: dict[str, Any] = {
        "user_report": user_report,
        "logs": raw_logs,
        "traces": raw_traces,
        "browser_errors": browser_errors,
    }

    try:
        # Lazy import to avoid circular: ingest_tool → ingest/normalizer → tools/__init__
        from src.ingest.normalizer import ingest as _run_ingest_pipeline

        evidence = _run_ingest_pipeline(raw_evidence)
    except Exception as exc:
        logger.warning("ingest_tool_normalize_failed", error=str(exc))
        return json.dumps(
            {"error": f"证据标准化失败: {exc}", "user_report": user_report},
            ensure_ascii=False,
        )

    # ── Phase 3: Format summary ──
    summary_text = _format_evidence_summary(evidence, user_report)

    # Also include machine-readable JSON for the agent
    result = {
        "summary": summary_text,
        "signal_count": len(getattr(evidence, "golden_signals", [])),
        "correlation_count": len(getattr(evidence, "correlations", [])),
        "timeline_event_count": len(getattr(evidence, "timeline", [])),
    }

    logger.info(
        "ingest_tool_complete",
        signal_count=result["signal_count"],
        correlation_count=result["correlation_count"],
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


# ── LangChain StructuredTool ─────────────────────────────────────────


def _build_ingest_tool() -> StructuredTool:
    """Build the LangChain StructuredTool for run_ingest."""
    return StructuredTool.from_function(
        coroutine=run_ingest,
        name="run_ingest",
        description=(
            "⭐ 诊断前必须先调用！采集可观测性数据（Loki 日志 + Tempo Trace）"
            "并标准化为结构化证据。\n"
            "参数：\n"
            "- user_report (必需): 用户对 Bug 的完整描述\n"
            "- trace_id (可选): 32位 hex Trace ID，用于精确查询\n"
            "- trigger_time (可选): ISO 格式时间，如 '2026-07-09T10:30:00'\n"
            "何时使用：\n"
            "- 用户描述了 Bug 但未提供 Trace ID → 用 trigger_time 或省略，"
            "工具用当前时间 ±5min 查询\n"
            "- 用户提供了 Trace ID → 传入 trace_id 精确查询\n"
            "返回 JSON：{summary, signal_count, correlation_count, timeline_event_count}\n"
            "调用后根据 summary 中的关键信号继续诊断。"
        ),
    )


# Module-level cached instance
_ingest_tool_cache: StructuredTool | None = None


def get_ingest_tool() -> StructuredTool:
    """Get or create the cached INGEST_TOOL."""
    global _ingest_tool_cache
    if _ingest_tool_cache is None:
        _ingest_tool_cache = _build_ingest_tool()
    return _ingest_tool_cache


INGEST_TOOL = get_ingest_tool()
