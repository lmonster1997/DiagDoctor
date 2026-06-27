"""
LangChain tools for the DiagDoctor agents.

Exposes observability data-fetching tools as LangChain StructuredTool instances
so they can be used directly inside ReAct agents (LangGraph prebuilt create_react_agent).

Usage:
    from src.tools import LOKI_QUERY_TOOL, TEMPO_TRACE_TOOL, TEMPO_SEARCH_TOOL

    agent = create_react_agent(
        model=llm,
        tools=[LOKI_QUERY_TOOL, TEMPO_TRACE_TOOL, TEMPO_SEARCH_TOOL],
        ...
    )
"""

from langchain_core.tools import StructuredTool

from src.tools.observability_tools import (
    query_loki_logs,
    query_tempo_trace,
    search_tempo_traces,
)
from src.tools.trace_query import (
    build_cross_tier_tree,
    detect_n_plus_one,
    find_bottlenecks,
    find_critical_path,
    find_error_spans,
    get_tree_summary,
)

# ── Loki log query tool ─────────────────────────────────────────────

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
        "The start and end parameters should be ISO-format datetime strings."
    ),
)

# ── Tempo trace query tool ──────────────────────────────────────────

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

# ── Tempo trace search tool ─────────────────────────────────────────

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

# ── Public API ──────────────────────────────────────────────────────

__all__ = [
    # Raw async functions (for direct use)
    "query_loki_logs",
    "query_tempo_trace",
    "search_tempo_traces",
    # Trace query / tree analysis (shared tools)
    "build_cross_tier_tree",
    "detect_n_plus_one",
    "find_bottlenecks",
    "find_critical_path",
    "find_error_spans",
    "get_tree_summary",
    # LangChain StructuredTool wrappers (for ReAct agents)
    "LOKI_QUERY_TOOL",
    "TEMPO_TRACE_TOOL",
    "TEMPO_SEARCH_TOOL",
]
