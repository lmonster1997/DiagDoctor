"""
LangChain tools for the DiagDoctor agents.

Exposes observability data-fetching tools as LangChain StructuredTool instances
so they can be used directly inside ReAct agents (LangChain create_agent).

Usage:
    from src.tools import LOKI_QUERY_TOOL, TEMPO_TRACE_TOOL, TEMPO_SEARCH_TOOL

    agent = create_agent(
        model=llm,
        tools=[LOKI_QUERY_TOOL, TEMPO_TRACE_TOOL, TEMPO_SEARCH_TOOL],
        ...
    )
"""

import json
from typing import Any

from langchain_core.tools import StructuredTool

from src.tools.code_search import CODE_SEARCH_TOOL
from src.tools.db_query import DB_QUERY_TOOL
from src.tools.file_reader import (
    get_file_content,
    get_get_file_content_tool,
)
from src.tools.frontend_inspect import (
    get_inspect_frontend_error_tool,
    inspect_frontend_error,
)
from src.tools.frontend_tools import (
    EXTRACT_STACK_TRACE_TOOL,
    FRONTEND_SPECIALIST_TOOLS,
    PARSE_BROWSER_ERRORS_TOOL,
    extract_stack_trace,
    parse_browser_errors,
)
from src.tools.observability_tools import (
    query_loki_logs,
    query_tempo_trace,
    search_tempo_traces,
)
from src.tools.observability_unified import (
    get_search_observability_tool,
    search_observability,
)
from src.tools.source_map_resolve import SOURCE_MAP_RESOLVE_TOOL
from src.tools.trace_query import (
    build_cross_tier_tree,
    detect_n_plus_one,
    find_bottlenecks,
    find_critical_path,
    find_error_spans,
    get_tree_summary,
)

# ── Lazy init for SEARCH_OBSERVABILITY_TOOL ─────────────────────────
# Deferred to first access to avoid import-time side effects.

SEARCH_OBSERVABILITY_TOOL = get_search_observability_tool()

# ── Lazy init for INSPECT_FRONTEND_ERROR_TOOL ───────────────────────

INSPECT_FRONTEND_ERROR_TOOL = get_inspect_frontend_error_tool()

# ── Lazy init for GET_FILE_CONTENT_TOOL ─────────────────────────────

GET_FILE_CONTENT_TOOL = get_get_file_content_tool()

# ── Loki log query tool (DEPRECATED: 使用 SEARCH_OBSERVABILITY_TOOL 替代) ──

LOKI_QUERY_TOOL = StructuredTool.from_function(
    coroutine=query_loki_logs,
    name="query_loki_logs",
    description=(
        "Query logs from Loki using LogQL syntax. "
        "Use this to search for error logs, warning messages, or any log pattern "
        "within a specified time range. "
        "LogQL examples: "
        '\'{service_name="demo-backend"} |= "error"\' for backend errors, '
        "'{service_name=\"demo-frontend\"}' for all frontend logs. "
        "The start and end parameters should be ISO-format datetime strings. "
        "IMPORTANT: Use a narrow time window (≤2 hours) around the evidence "
        "timestamps. If the evidence contains timestamps, center your query "
        "±1 hour around them. Never use multi-day ranges — Loki will reject them."
    ),
)

# ── Tempo trace query tool (DEPRECATED: 使用 SEARCH_OBSERVABILITY_TOOL 替代) ──

TEMPO_TRACE_TOOL = StructuredTool.from_function(
    coroutine=query_tempo_trace,
    name="query_tempo_trace",
    description=(
        "Retrieve the complete trace (all spans) for a given trace ID from Tempo. "
        "Use this when you have a specific trace_id from logs and want to see "
        "the full distributed trace including all service calls, database queries, etc. "
        "The trace_id is a 32-character hex string."
    ),
)

# ── Tempo trace search tool (DEPRECATED: 使用 SEARCH_OBSERVABILITY_TOOL 替代) ──

TEMPO_SEARCH_TOOL = StructuredTool.from_function(
    coroutine=search_tempo_traces,
    name="search_tempo_traces",
    description=(
        "Search for traces in Tempo by service name and time range. "
        "Use this to discover relevant traces when you don't have a specific trace ID. "
        "Optionally filter by minimum duration to find slow traces. "
        "Returns a list of trace summaries with trace_id, root_service, duration_ms, etc. "
        "The start and end parameters should be ISO-format datetime strings."
    ),
)

# ── Trace analysis tool (DEPRECATED: 使用 SEARCH_OBSERVABILITY_TOOL(analysis="full") 替代) ──


async def analyze_trace_tree(trace_id: str) -> str:
    """
    Fetch a complete trace by trace_id, build the span tree, and return a
    structured analysis summary.

    The summary includes:
    - N+1 query patterns (if >=3 repeated DB statements under same parent)
    - Bottleneck spans (slowest spans above 200ms threshold)
    - Error spans (status=error)
    - Cross-tier structure (frontend vs backend span counts)
    - Critical path (longest cumulative path through tree)

    Args:
        trace_id: The 32-character hex trace ID to analyze.

    Returns:
        JSON string with the full tree summary (see ``get_tree_summary``).
    """
    from src.graph.state import TraceSpan

    spans: list[TraceSpan] | list[dict[str, Any]] = await query_tempo_trace(trace_id)
    if not spans:
        return json.dumps(
            {"error": "No spans found for this trace_id", "trace_id": trace_id},
            ensure_ascii=False,
        )

    # Normalise: Pydantic TraceSpan → dict for tree builder
    dict_spans: list[dict[str, Any]] = []
    for s in spans:
        if isinstance(s, dict):
            dict_spans.append(s)
        else:
            dict_spans.append(s.model_dump())

    roots = build_cross_tier_tree(dict_spans)
    summary = get_tree_summary(roots)
    return json.dumps(summary, ensure_ascii=False, indent=2)


TRACE_ANALYSIS_TOOL = StructuredTool.from_function(
    coroutine=analyze_trace_tree,
    name="analyze_trace",
    description=(
        "Fetch the complete trace for a trace_id and automatically analyze it. "
        "Returns: total spans, frontend/backend counts, N+1 query patterns "
        "(repeated DB statements), bottleneck spans (slowest), error spans, "
        "and the critical path. "
        "Use this when you need to understand trace structure and identify "
        "performance issues such as N+1 queries, slow database calls, or "
        "error propagation across services. "
        "The trace_id is a 32-character hex string."
    ),
)

# ── V3 统一工具集 (5 tools for UnifiedAgent) ────────────────────────

_ALL_TOOLS_CACHE: list[StructuredTool] | None = None


def _build_all_tools() -> list[StructuredTool]:
    """Build the V3 ALL_TOOLS list."""
    return [
        SEARCH_OBSERVABILITY_TOOL,  # 新：统一可观测性查询
        CODE_SEARCH_TOOL,  # 保留：语义代码搜索
        DB_QUERY_TOOL,  # 保留：只读数据库查询
        INSPECT_FRONTEND_ERROR_TOOL,  # 新：一站式前端分析
        GET_FILE_CONTENT_TOOL,  # 新：文件读取
    ]


def get_all_tools() -> list[StructuredTool]:
    """Get the V3 unified tool set (5 tools). Cached after first call."""
    global _ALL_TOOLS_CACHE
    if _ALL_TOOLS_CACHE is None:
        _ALL_TOOLS_CACHE = _build_all_tools()
    return _ALL_TOOLS_CACHE


# Module-level alias for convenience
ALL_TOOLS = get_all_tools()


# ── V2 兼容别名 (过渡期保留) ─────────────────────────────────────────

# SHARED_TOOLS 保留旧 7 工具集，确保现有 specialist agent 不中断。
# V3 新代码应使用 ALL_TOOLS / get_all_tools()。
SHARED_TOOLS: list[StructuredTool] = [
    CODE_SEARCH_TOOL,
    DB_QUERY_TOOL,
    LOKI_QUERY_TOOL,  # DEPRECATED
    TEMPO_TRACE_TOOL,  # DEPRECATED
    TEMPO_SEARCH_TOOL,  # DEPRECATED
    TRACE_ANALYSIS_TOOL,  # DEPRECATED
    SOURCE_MAP_RESOLVE_TOOL,
]

# ── Public API ──────────────────────────────────────────────────────

__all__ = [
    # Raw async functions (for direct use)
    "query_loki_logs",
    "query_tempo_trace",
    "search_tempo_traces",
    "analyze_trace_tree",
    "search_observability",
    "inspect_frontend_error",
    "get_file_content",
    "parse_browser_errors",
    "extract_stack_trace",
    # Trace query / tree analysis (shared tools)
    "build_cross_tier_tree",
    "detect_n_plus_one",
    "find_bottlenecks",
    "find_critical_path",
    "find_error_spans",
    "get_tree_summary",
    # LangChain StructuredTool wrappers (for ReAct agents)
    "CODE_SEARCH_TOOL",
    "DB_QUERY_TOOL",
    "EXTRACT_STACK_TRACE_TOOL",
    "ALL_TOOLS",  # V3 统一工具集 (5 tools)
    "FRONTEND_SPECIALIST_TOOLS",  # DEPRECATED: 使用 INSPECT_FRONTEND_ERROR_TOOL 替代
    "GET_FILE_CONTENT_TOOL",  # V3 文件读取
    "INSPECT_FRONTEND_ERROR_TOOL",  # V3 一站式前端分析
    "LOKI_QUERY_TOOL",  # DEPRECATED: 使用 SEARCH_OBSERVABILITY_TOOL 替代
    "PARSE_BROWSER_ERRORS_TOOL",
    "SEARCH_OBSERVABILITY_TOOL",  # V3 统一可观测性入口
    "SHARED_TOOLS",  # V2 兼容 (7 tools)
    "SOURCE_MAP_RESOLVE_TOOL",
    "TEMPO_SEARCH_TOOL",  # DEPRECATED: 使用 SEARCH_OBSERVABILITY_TOOL 替代
    "TEMPO_TRACE_TOOL",  # DEPRECATED: 使用 SEARCH_OBSERVABILITY_TOOL 替代
    "TRACE_ANALYSIS_TOOL",  # DEPRECATED: 使用 SEARCH_OBSERVABILITY_TOOL 替代
    # V3 工具集构建函数
    "get_all_tools",
    "get_search_observability_tool",
    "get_inspect_frontend_error_tool",
    "get_get_file_content_tool",
]
