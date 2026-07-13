"""
Source map resolve tool — resolve minified/compiled JS locations to original source.

Provides a LangChain StructuredTool for ReAct agents to map frontend
stack traces from minified bundles back to original TypeScript/JSX source
using the source maps archived with each bug case.

Source maps are stored in:
    bug-factory/output/{recipe_id}/sourcemaps/

Usage:
    source_map_resolve("TaskBoard.tsx", 42)
"""

from __future__ import annotations

import json

from langchain_core.tools import StructuredTool

from src.observability.logger import get_logger
from src.observability.tracing import traced

logger = get_logger(__name__)


@traced()
async def source_map_resolve(file: str, line: int) -> str:
    """
    Resolve a compiled/bundled file location to its original source.

    Args:
        file: The file path from a minified stack trace
              (e.g. 'assets/index-BxY9Z.js' or relative path like 'TaskBoard.tsx').
        line: The line number from the stack trace.

    Returns:
        JSON string with original file path, line, column, and optional
        function name from the source map.
    """

    try:
        # For now, return a structured placeholder — full source map
        # resolution will be wired once code_index + sourcemaps are in place.
        result = {
            "original_file": file,
            "original_line": line,
            "original_column": 0,
            "original_name": "",
            "note": "source_map_resolve: full source map resolution pending. "
            "Returning input as-is.",
            "status": "passthrough",
        }

        logger.info("source_map_resolve_called", file=file, line=line)
        return json.dumps(result, ensure_ascii=False)

    except Exception as exc:
        logger.error("source_map_resolve_failed", error=str(exc))
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


# ── LangChain StructuredTool wrapper ─────────────────────────────────

SOURCE_MAP_RESOLVE_TOOL = StructuredTool.from_function(
    coroutine=source_map_resolve,
    name="source_map_resolve",
    description=(
        "Resolve a compiled/minified JavaScript file path and line number "
        "back to the original TypeScript/JSX source using source maps. "
        "Use this when a frontend stack trace references a minified bundle file. "
        "Example: file='assets/index-BxY9Z.js', line=142"
    ),
)
