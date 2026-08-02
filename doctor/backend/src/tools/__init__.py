"""
LangChain tools for the DiagDoctor diagnosis agent (V3 unified set).

Exposes the 6 diagnostic tools + 1 §7.2 record_hypothesis 埋点工具 as
LangChain StructuredTool instances for the ``create_agent`` ReAct agent.

Usage:
    from src.tools import get_all_tools

    agent = create_agent(model=llm, tools=get_all_tools(), ...)
"""

from langchain_core.tools import StructuredTool

from src.tools.code_search import CODE_SEARCH_TOOL
from src.tools.db_query import DB_QUERY_TOOL
from src.tools.file_reader import get_file_content, get_get_file_content_tool
from src.tools.frontend_inspect import get_inspect_frontend_error_tool, inspect_frontend_error
from src.tools.hypothesis_log import RECORD_HYPOTHESIS_TOOL, record_hypothesis
from src.tools.memory_recall import ROOT_CAUSE_RECALL_TOOL, search_historical_root_cause
from src.tools.observability_tools import query_loki_logs, query_tempo_trace, search_tempo_traces
from src.tools.observability_unified import get_search_observability_tool, search_observability

# ── Lazy init (avoid import-time side effects) ──────────────────────

SEARCH_OBSERVABILITY_TOOL = get_search_observability_tool()
INSPECT_FRONTEND_ERROR_TOOL = get_inspect_frontend_error_tool()
GET_FILE_CONTENT_TOOL = get_get_file_content_tool()


# ── V3 统一工具集 (6 诊断工具 + 1 §7.2 record_hypothesis 埋点工具) ─────

_ALL_TOOLS_CACHE: list[StructuredTool] | None = None


def _build_all_tools() -> list[StructuredTool]:
    """Build the V3 ALL_TOOLS list.

    6 诊断工具(可观测性/代码/db/前端/文件/历史根因) + 1 §7.2 假设证伪埋点工具
    (``record_hypothesis``)。埋点工具是 no-op,预算豁免(见 BudgetGuardMiddleware),
    不吃诊断 ``MAX_MODEL_CALLS`` cap。
    """
    return [
        SEARCH_OBSERVABILITY_TOOL,  # 统一可观测性查询
        CODE_SEARCH_TOOL,  # 语义代码搜索
        DB_QUERY_TOOL,  # 只读数据库查询
        INSPECT_FRONTEND_ERROR_TOOL,  # 一站式前端分析
        GET_FILE_CONTENT_TOOL,  # 文件读取
        ROOT_CAUSE_RECALL_TOOL,  # P1-a: 根因向量检索历史相似 bug (§6.4)
        RECORD_HYPOTHESIS_TOOL,  # §7.2: 假设证伪埋点(预算豁免)
    ]


def get_all_tools() -> list[StructuredTool]:
    """Get the V3 unified tool set (6 诊断 + 1 埋点). Cached after first call."""
    global _ALL_TOOLS_CACHE
    if _ALL_TOOLS_CACHE is None:
        _ALL_TOOLS_CACHE = _build_all_tools()
    return _ALL_TOOLS_CACHE


# Module-level alias for convenience
ALL_TOOLS = get_all_tools()

# ── Public API ──────────────────────────────────────────────────────

__all__ = [
    # Raw async functions (for direct use)
    "query_loki_logs",
    "query_tempo_trace",
    "search_tempo_traces",
    "search_observability",
    "inspect_frontend_error",
    "get_file_content",
    "search_historical_root_cause",
    "record_hypothesis",
    # LangChain StructuredTool wrappers (for ReAct agents)
    "CODE_SEARCH_TOOL",
    "DB_QUERY_TOOL",
    "ALL_TOOLS",  # V3 统一工具集 (6 诊断 + 1 埋点)
    "GET_FILE_CONTENT_TOOL",
    "INSPECT_FRONTEND_ERROR_TOOL",
    "ROOT_CAUSE_RECALL_TOOL",  # P1-a: 根因向量历史检索 (§6.4)
    "RECORD_HYPOTHESIS_TOOL",  # §7.2: 假设证伪埋点工具
    "SEARCH_OBSERVABILITY_TOOL",  # V3 统一可观测性入口
    # V3 工具集构建函数
    "get_all_tools",
    "get_search_observability_tool",
    "get_inspect_frontend_error_tool",
    "get_get_file_content_tool",
]
