"""
Code search tool — wraps the KnowledgeService semantic code search.

Provides a LangChain StructuredTool for ReAct agents to search the
demo-app codebase index by semantic similarity.

The code index (Qdrant collection: code_index) must be pre-built
via ``python -m doctor.scripts.init_kb`` or the code_index CLI.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from src.knowledge.hybrid_service import get_knowledge_service
from src.observability.logger import get_logger
from src.observability.tracing import traced

logger = get_logger(__name__)


@traced()
async def code_search(query: str, k: int = 5) -> str:
    """
    Semantic search over the demo-app codebase index.

    Args:
        query: Natural language description or code snippet to search for.
               Examples: "list_tasks function", "N+1 query in task service",
               "SQLAlchemy joinedload"
        k: Number of results to return (default 5, max 10).

    Returns:
        JSON string with search results including file paths, function names,
        line numbers, and code snippets.
    """
    k = min(max(1, k), 10)  # clamp to [1, 10]

    try:
        svc = get_knowledge_service()
        docs = await svc.search_code(query, k=k)

        if not docs:
            logger.info("code_search_no_results", query=query[:100])
            return "[]"

        results: list[dict[str, Any]] = []
        for doc in docs:
            meta = doc.metadata or {}
            results.append(
                {
                    "file_path": meta.get("file_path", "unknown"),
                    "name": meta.get("name", ""),
                    "chunk_type": meta.get("chunk_type", "unknown"),
                    "start_line": meta.get("start_line", 0),
                    "end_line": meta.get("end_line", 0),
                    "language": meta.get("language", "python"),
                    "score": meta.get("_score", 0.0),
                    "content": doc.page_content[:2000],  # truncate
                }
            )

        import json

        logger.info("code_search_completed", query=query[:100], result_count=len(results))
        return json.dumps(results, ensure_ascii=False)

    except Exception as exc:
        logger.error("code_search_failed", error=str(exc))
        return f"Error: {exc}"


# ── LangChain StructuredTool wrapper ─────────────────────────────────

CODE_SEARCH_TOOL = StructuredTool.from_function(
    coroutine=code_search,
    name="code_search",
    description=(
        "Search the demo-app codebase for relevant code snippets using semantic search. "
        "Use this to locate the exact file, function, or code block related to a bug. "
        "Examples: "
        "query='list_tasks function SQLAlchemy' to find task listing code; "
        "query='N+1 query joinedload' to find missing eager loading. "
        "Returns JSON with file_path, name, line numbers, and code content."
    ),
)
