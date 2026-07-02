"""
统一可观测性查询入口 (V3)。

将 ``query_loki_logs`` + ``search_tempo_traces`` + ``query_tempo_trace`` +
``analyze_trace`` 合并为一个统一入口 ``search_observability``。

设计原则:
- source="auto" 时: 先查 Loki → 提取 trace_id → 自动调 Tempo
- analysis="full" 时: 自动做 N+1 检测、瓶颈分析、错误 span 提取
- 时间范围安全校验: 跨度不超过 4 小时
- 返回统一的 JSON 结构: {logs: [...], traces: [...], analysis: {...}}

Usage:
    from src.tools.observability_unified import search_observability

    result = await search_observability(
        source="auto",
        query='{service_name="demo-backend"} |= "error"',
        start="2026-06-28T10:00:00Z",
        end="2026-06-28T14:00:00Z",
        analysis="full",
    )
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from src.observability.logger import get_logger
from src.observability.tracing import traced
from src.tools.observability_tools import (
    query_loki_logs,
    query_tempo_trace,
    search_tempo_traces,
)
from src.tools.trace_query import (
    SpanNode,
    build_cross_tier_tree,
    detect_n_plus_one,
    find_bottlenecks,
    find_error_spans,
    get_tree_summary,
)

logger = get_logger(__name__)

# ── Constants ───────────────────────────────────────────────────────

# Maximum time range for search_observability (stricter than Loki's 24h)
MAX_QUERY_RANGE_HOURS: float = 4.0

# Maximum log entries to fetch in auto mode
AUTO_MODE_LOG_LIMIT: int = 500

# Maximum trace IDs to follow up in auto mode
AUTO_MODE_MAX_TRACE_IDS: int = 5

# Trace ID extraction regex (32-char hex string)
_TRACE_ID_RE = re.compile(r"\b([0-9a-fA-F]{32})\b")


# ── Helper: Time range parsing & validation ─────────────────────────


def _parse_time(time_str: str | None) -> datetime | None:
    """Parse an ISO-format time string, or return None if empty/None."""
    if not time_str:
        return None
    try:
        # Handle 'Z' suffix
        normalized = time_str.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        return None


def _validate_time_range(start: datetime, end: datetime) -> None:
    """Raise ValueError if the time range exceeds MAX_QUERY_RANGE_HOURS."""
    diff_hours = (end - start).total_seconds() / 3600.0
    if diff_hours > MAX_QUERY_RANGE_HOURS:
        raise ValueError(
            f"Time range exceeds maximum allowed span of {MAX_QUERY_RANGE_HOURS:.0f} hours "
            f"(requested: {diff_hours:.1f}h). "
            f"Please narrow your start/end range. "
            f"start={start.isoformat()}, end={end.isoformat()}"
        )
    if start >= end:
        raise ValueError(
            f"start must be before end: start={start.isoformat()}, end={end.isoformat()}"
        )


def _default_time_range() -> tuple[datetime, datetime]:
    """Return a default time range (last 1 hour) when start/end not provided."""
    now = datetime.now(tz=UTC)
    return now - timedelta(hours=1), now


# ── Helper: Trace ID extraction from logs ────────────────────────────


def _extract_trace_ids_from_logs(logs: list[dict[str, Any]]) -> list[str]:
    """Extract unique trace_ids from log entries.

    Looks for trace_id in:
    1. Explicit trace_id field in log entry
    2. trace_id embedded in message text (32-char hex pattern)
    """
    trace_ids: list[str] = []
    seen: set[str] = set()

    for entry in logs:
        # Check explicit trace_id field
        tid = entry.get("trace_id")
        if tid and isinstance(tid, str) and len(tid) >= 32:
            clean = tid.strip()[:32].lower()
            if clean not in seen and re.match(r"^[0-9a-f]{32}$", clean):
                seen.add(clean)
                trace_ids.append(clean)
                continue

        # Search message text for trace IDs
        message = entry.get("message", "")
        matches = _TRACE_ID_RE.findall(message)
        for m in matches:
            clean = m.lower()
            if clean not in seen:
                seen.add(clean)
                trace_ids.append(clean)

    return trace_ids[:AUTO_MODE_MAX_TRACE_IDS]


# ── Helper: Parse log entries to dict ────────────────────────────────


def _log_entry_to_dict(entry: Any) -> dict[str, Any]:
    """Convert a LogEntry (Pydantic or dict) to a plain dict for JSON output."""
    if isinstance(entry, dict):
        result = dict(entry)
        # Normalise datetime objects
        for key in ("timestamp",):
            if key in result and isinstance(result[key], datetime):
                result[key] = result[key].isoformat()
        return result
    # Pydantic model
    try:
        d: dict[str, Any] = entry.model_dump()
        for key in ("timestamp",):
            if key in d and isinstance(d[key], datetime):
                d[key] = d[key].isoformat()
        return d
    except AttributeError:
        return {"raw": str(entry)}


def _span_to_dict(span: Any) -> dict[str, Any]:
    """Convert a TraceSpan (Pydantic or dict) to a plain dict for JSON output."""
    if isinstance(span, dict):
        result = dict(span)
        for key in ("start", "timestamp"):
            if key in result and isinstance(result[key], datetime):
                result[key] = result[key].isoformat()
        return result
    try:
        d: dict[str, Any] = span.model_dump()
        for key in ("start", "timestamp"):
            if key in d and isinstance(d[key], datetime):
                d[key] = d[key].isoformat()
        return d
    except AttributeError:
        return {"raw": str(span)}


# ── Helper: Run trace analysis ───────────────────────────────────────


def _run_trace_analysis(
    traces: list[dict[str, Any]],
    analysis: Literal["raw", "n_plus_one", "bottlenecks", "errors", "full"],
) -> dict[str, Any]:
    """Run the requested analysis on trace data.

    Args:
        traces: Flat list of trace span dicts.
        analysis: What analysis to run.

    Returns:
        Analysis result dict, possibly with sub-keys like
        n_plus_one, bottlenecks, error_spans, critical_path.
    """
    if not traces:
        return {"note": "No trace data to analyze"}

    roots: list[SpanNode] = build_cross_tier_tree(traces)

    result: dict[str, Any] = {"span_count": len(traces), "root_count": len(roots)}

    if analysis in ("n_plus_one", "full"):
        result["n_plus_one"] = detect_n_plus_one(roots)

    if analysis in ("bottlenecks", "full"):
        result["bottlenecks"] = find_bottlenecks(roots)

    if analysis in ("errors", "full"):
        result["error_spans"] = find_error_spans(roots)

    if analysis == "full":
        summary = get_tree_summary(roots)
        result["summary"] = summary

    return result


# ── Public API ──────────────────────────────────────────────────────


@traced("observability.search_observability")
async def search_observability(
    source: Literal["loki", "tempo", "auto"],
    query: str,
    start: str | None = None,
    end: str | None = None,
    analysis: Literal["raw", "n_plus_one", "bottlenecks", "errors", "full"] = "full",
    limit: int = 20,
    include_frontend: bool = False,
) -> str:
    """统一可观测性查询入口。

    根据 ``source`` 参数决定查询策略:
    - ``source="loki"``: 仅查询 Loki 日志
    - ``source="tempo"``: 查询 Tempo trace（query 为 trace_id 时直接查 trace，
      否则按服务名搜索 traces）
    - ``source="auto"``: 先查 Loki 获取日志和 trace_id，再自动关联查 Tempo

    根据 ``analysis`` 参数决定分析深度:
    - ``"raw"``: 仅返回原始数据
    - ``"n_plus_one"``: 仅做 N+1 检测
    - ``"bottlenecks"``: 仅做瓶颈分析
    - ``"errors"``: 仅提取错误 span
    - ``"full"``: 全部分析

    Args:
        source: 数据源 — "loki" | "tempo" | "auto"
        query: 查询字符串。source="loki" 时为 LogQL；
               source="tempo" 时为 trace_id 或服务名；
               source="auto" 时为 LogQL
        start: ISO 格式起始时间（可选，默认为 1 小时前）
        end: ISO 格式结束时间（可选，默认为当前时间）
        analysis: 分析深度（仅在使用 trace 数据时生效）
        limit: 最大返回条数
        include_frontend: 是否同时查询前端服务（demo-frontend）的错误。
            当设为 True 时，额外查询前端 Loki 日志和 Tempo 中的
            client_error span，提取到 frontend_errors 字段。
            适用于前端白屏/崩溃类 Bug 诊断。

    Returns:
        JSON 字符串，结构为:
        {
            "source": str,
            "query": str,
            "time_range": {"start": "...", "end": "..."},
            "logs": [...],
            "traces": [...],
            "analysis": {...},
            "metadata": {"auto_trace_ids": [...], ...},
            "frontend_errors": [{"name": "client_error", "message": "...", ...}, ...]
        }
    """
    # ── Parse and validate time range ──
    parsed_start = _parse_time(start)
    parsed_end = _parse_time(end)

    if parsed_start is None or parsed_end is None:
        parsed_start, parsed_end = _default_time_range()
        logger.info(
            "search_observability_default_time_range",
            start=parsed_start.isoformat(),
            end=parsed_end.isoformat(),
        )
    else:
        _validate_time_range(parsed_start, parsed_end)

    # ── Execute queries based on source ──
    logs: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}

    if source in ("loki", "auto"):
        # Query Loki logs
        try:
            raw_logs = await query_loki_logs(
                logql=query,
                start=parsed_start,
                end=parsed_end,
                limit=AUTO_MODE_LOG_LIMIT if source == "auto" else limit * 10,
            )
            logs = [_log_entry_to_dict(e) for e in raw_logs]
            logger.info(
                "search_observability_loki_done",
                source=source,
                log_count=len(logs),
            )
        except Exception as exc:
            logger.error("search_observability_loki_failed", error=str(exc))
            logs = []

        # Auto mode: extract trace_ids and query Tempo
        if source == "auto" and logs:
            trace_ids = _extract_trace_ids_from_logs(logs)
            metadata["auto_trace_ids"] = trace_ids
            logger.info(
                "search_observability_auto_trace_ids",
                count=len(trace_ids),
                trace_ids=trace_ids,
            )

            for tid in trace_ids:
                try:
                    raw_spans = await query_tempo_trace(tid)
                    spans = [_span_to_dict(s) for s in raw_spans]
                    traces.extend(spans)
                    logger.info(
                        "search_observability_auto_tempo_done",
                        trace_id=tid,
                        span_count=len(spans),
                    )
                except Exception as exc:
                    logger.warning(
                        "search_observability_auto_tempo_failed",
                        trace_id=tid,
                        error=str(exc),
                    )
                    continue

    elif source == "tempo":
        # Determine if query is a trace_id (32-char hex) or service name
        if re.match(r"^[0-9a-fA-F]{32}$", query.strip()):
            # Direct trace lookup
            try:
                raw_spans = await query_tempo_trace(query.strip())
                traces = [_span_to_dict(s) for s in raw_spans]
                metadata["trace_id"] = query.strip()
                logger.info(
                    "search_observability_tempo_trace_done",
                    trace_id=query.strip(),
                    span_count=len(traces),
                )
            except Exception as exc:
                logger.error(
                    "search_observability_tempo_trace_failed",
                    trace_id=query.strip(),
                    error=str(exc),
                )
                traces = []
        else:
            # Search by service name
            try:
                trace_summaries = await search_tempo_traces(
                    service=query.strip(),
                    start=parsed_start,
                    end=parsed_end,
                )
                metadata["tempo_search_results"] = trace_summaries
                logger.info(
                    "search_observability_tempo_search_done",
                    service=query,
                    result_count=len(trace_summaries),
                )
            except Exception as exc:
                logger.error(
                    "search_observability_tempo_search_failed",
                    service=query,
                    error=str(exc),
                )
                metadata["tempo_search_results"] = []

    # ── Run analysis on trace data ──
    analysis_result: dict[str, Any] = {}
    if traces and analysis != "raw":
        try:
            analysis_result = _run_trace_analysis(traces, analysis)
            logger.info(
                "search_observability_analysis_done",
                analysis=analysis,
                keys=list(analysis_result.keys()),
            )
        except Exception as exc:
            logger.error("search_observability_analysis_failed", error=str(exc))
            analysis_result = {"error": str(exc)}

    # ── Include frontend errors (optional) ────────────────────────
    frontend_errors: list[dict[str, Any]] = []
    if include_frontend:
        try:
            fe_logql = '{service_name=~"demo-frontend"}'
            fe_logs_raw = await query_loki_logs(
                logql=fe_logql,
                start=parsed_start,
                end=parsed_end,
                limit=AUTO_MODE_LOG_LIMIT,
            )
            fe_logs = [_log_entry_to_dict(e) for e in fe_logs_raw]
            fe_trace_ids = _extract_trace_ids_from_logs(fe_logs)

            for tid in fe_trace_ids[:AUTO_MODE_MAX_TRACE_IDS]:
                try:
                    raw_spans = await query_tempo_trace(tid)
                    fe_spans = [_span_to_dict(s) for s in raw_spans]
                    for span in fe_spans:
                        name = span.get("name", "")
                        if "client_error" in name or span.get("status") == "error":
                            attrs = span.get("attributes", {})
                            frontend_errors.append(
                                {
                                    "span_name": name,
                                    "trace_id": tid,
                                    "span_id": span.get("span_id", ""),
                                    "duration_ms": span.get("duration_ms", 0),
                                    "error_message": (
                                        attrs.get("error.message") or attrs.get("error", "")
                                    ),
                                    "error_stack": attrs.get("error.stack", ""),
                                    "component_stack": attrs.get("component_stack", ""),
                                    "timestamp": span.get("start", ""),
                                }
                            )
                    # Also add all frontend spans to the main traces
                    traces.extend(fe_spans)
                except Exception as fe_exc:
                    logger.debug(
                        "frontend_error_trace_fetch_failed",
                        trace_id=tid,
                        error=str(fe_exc),
                    )

            # Add frontend logs to main logs
            logs.extend(fe_logs)
            logger.info(
                "search_observability_frontend_errors",
                count=len(frontend_errors),
                log_count=len(fe_logs),
            )
        except Exception as fe_exc:
            logger.warning("search_observability_frontend_failed", error=str(fe_exc))

    # ── Assemble response ──
    response = {
        "source": source,
        "query": query,
        "time_range": {
            "start": parsed_start.isoformat(),
            "end": parsed_end.isoformat(),
        },
        "logs": logs[:limit],
        "traces": traces[:limit],
        "analysis": analysis_result,
        "metadata": metadata,
        "frontend_errors": frontend_errors[:limit],
    }

    # Truncate large payloads for LLM context
    result_json = json.dumps(response, ensure_ascii=False, indent=2, default=str)
    original_json = result_json  # keep reference for accurate comparison

    if len(result_json) > 8000:
        # Truncate logs and traces arrays, keep analysis
        truncated: dict[str, Any] = dict(response)
        truncated["logs"] = logs[:5]
        truncated["traces"] = traces[:5]
        truncated["_truncated"] = True
        truncated["_original_counts"] = {
            "logs": len(logs),
            "traces": len(traces),
        }
        result_json = json.dumps(truncated, ensure_ascii=False, indent=2, default=str)
        logger.warning(
            "search_observability_truncated",
            original_size=len(original_json),
            truncated_size=len(result_json),
            reduction_pct=round((1 - len(result_json) / len(original_json)) * 100, 1),
        )

    return result_json


# ── LangChain StructuredTool ─────────────────────────────────────────


def _build_search_observability_tool() -> Any:
    """Build the LangChain StructuredTool for search_observability."""
    from langchain_core.tools import StructuredTool

    return StructuredTool.from_function(
        coroutine=search_observability,
        name="search_observability",
        description=(
            "统一可观测性查询入口 ⭐ 优先使用。\n"
            "支持三种模式：\n"
            "1. 查日志: search_observability(source='loki', "
            'query=\'{service_name="demo-backend"} |= "error"\', '
            "start='...', end='...')\n"
            "2. 查Trace: search_observability(source='tempo', "
            "query='<trace_id>')\n"
            "3. 自动关联: search_observability(source='auto', "
            "query='<LogQL>', analysis='full') — 先查Loki获取日志和trace_id，"
            "再自动关联查Tempo，并进行N+1/瓶颈/错误分析\n"
            "analysis 参数: 'raw'(仅原始数据), 'n_plus_one'(N+1检测), "
            "'bottlenecks'(瓶颈分析), 'errors'(错误span), 'full'(全部分析)\n"
            "include_frontend=True: 同时查询前端 client_error span 和日志，"
            "适用于前端白屏/崩溃类 Bug\n"
            "时间范围跨度不能超过4小时。返回JSON: "
            "{logs:..., traces:..., analysis:..., frontend_errors:...}"
        ),
    )


# Deferred construction: built on first access via __init__.py
_search_obs_tool_cache: Any = None


def get_search_observability_tool() -> Any:
    """Get or create the cached SEARCH_OBSERVABILITY_TOOL."""
    global _search_obs_tool_cache
    if _search_obs_tool_cache is None:
        _search_obs_tool_cache = _build_search_observability_tool()
    return _search_obs_tool_cache


# ── Public API ──────────────────────────────────────────────────────

__all__ = [
    "search_observability",
    "get_search_observability_tool",
]
