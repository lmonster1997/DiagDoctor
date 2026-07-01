"""
Code search tool — ripgrep exact search.

Strategy:
1. ripgrep exact/keyword match (fast, precise)
2. When no match: return structured suggestions to guide Agent
   toward ``get_file_content`` or ``search_observability`` instead
   of pretending semantic search works for code.

ripgrep is optional — if ``rg`` is not installed the tool silently
degrades to returning a helpful fallback message.

Design rationale: RAG/vector search for code is a false safety net —
embedding similarity does not map well to code identifiers.  The
Agent's correct path is ``search_observability → get clue → ripgrep``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any

from langchain_core.tools import StructuredTool

from src.config import settings
from src.observability.tracing import traced

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────

# File types ripgrep searches (limits to relevant source files).
# These are ripgrep built-in type names (see: rg --type-list).
#   py   → *.py, *.pyi
#   ts   → *.ts, *.tsx, *.cts, *.mts
#   js   → *.js, *.jsx, *.cjs, *.mjs
RG_FILE_TYPES: list[str] = ["py", "ts", "js", "json", "yaml", "sql", "md"]

# Max ripgrep execution time (seconds)
RG_TIMEOUT: float = 10.0

# How many lines of context ripgrep provides around each match
RG_CONTEXT_LINES: int = 3

# Source roots we search (relative to project root)
SEARCH_ROOTS: list[str] = ["demo-app"]


# ── Helpers ────────────────────────────────────────────────────────────


def _rg_available() -> bool:
    """Check whether the ``rg`` binary is on PATH."""
    return shutil.which("rg") is not None


def _classify_file_role(file_path: str) -> str:
    """Assign a semantic role label to a source file based on its path.

    Used by both ripgrep and vector result paths so the agent can
    quickly understand *what kind* of code it is looking at.
    """
    path_lower = file_path.lower().replace("\\", "/")

    # Ordered from most-specific to most-generic
    if "/api/" in path_lower or "/routes/" in path_lower or path_lower.endswith("_router.py"):
        return "api_route"
    if "/services/" in path_lower or "_service.py" in path_lower:
        return "business_logic"
    if "/models/" in path_lower or "/schemas/" in path_lower or "/entities/" in path_lower:
        return "data_model"
    if "/middleware/" in path_lower or "_middleware.py" in path_lower:
        return "middleware"
    if "/auth/" in path_lower or "_auth" in path_lower:
        return "auth"
    if (
        "/config" in path_lower
        or path_lower.endswith("settings.py")
        or path_lower.endswith("config.py")
    ):
        return "config"
    if "/pages/" in path_lower or "/views/" in path_lower:
        return "frontend_page"
    if "/components/" in path_lower:
        return "frontend_component"
    if "/stores/" in path_lower or "/hooks/" in path_lower or "/composables/" in path_lower:
        return "frontend_state"
    if "/services/" in path_lower and (".ts" in path_lower or ".tsx" in path_lower):
        return "frontend_service"
    if "/migrations/" in path_lower or "/alembic/" in path_lower:
        return "db_migration"
    if "/tests/" in path_lower or "/__tests__/" in path_lower or path_lower.endswith("_test.py"):
        return "test"
    if "/recipes/" in path_lower or "/bug_factory/" in path_lower:
        return "bug_recipe"
    if ".py" in path_lower:
        return "python_module"
    if any(path_lower.endswith(ext) for ext in (".ts", ".tsx", ".js", ".jsx")):
        return "frontend_script"
    if path_lower.endswith(".sql"):
        return "sql_script"
    return "unknown"


# ── ripgrep search ─────────────────────────────────────────────────────


async def _ripgrep_search(query: str, k: int = 10) -> list[dict[str, Any]]:
    """Run ripgrep and return structured match results.

    Attempts whole-word match first (``-w``); falls back to substring match.
    Returns an empty list when ripgrep is unavailable or finds nothing.
    """
    if not _rg_available():
        logger.debug("ripgrep not found on PATH — skipping")
        return []

    search_roots = _resolve_search_roots()
    if not search_roots:
        logger.warning("No search roots exist on disk — skipping ripgrep")
        return []

    # Build file-type args
    type_args: list[str] = []
    for ft in RG_FILE_TYPES:
        type_args.extend(["--type", ft])

    for attempt, use_word in enumerate([True, False]):
        cmd: list[str] = [
            "rg",
            "--json",  # machine-parseable output
            "--line-number",  # include line numbers
            "--context",
            str(RG_CONTEXT_LINES),
            "--no-heading",
            "--color",
            "never",
            *type_args,
        ]
        if use_word:
            cmd.append("--word-regexp")
        cmd.append("--")  # end of options
        cmd.append(query)  # the search pattern
        cmd.extend(search_roots)  # paths to search

        logger.debug("ripgrep attempt %d: %s", attempt + 1, " ".join(cmd))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=RG_TIMEOUT
            )
        except TimeoutError:
            logger.warning("ripgrep timed out after %.1fs", RG_TIMEOUT)
            return []
        except FileNotFoundError:
            logger.debug("ripgrep binary not found")
            return []
        except Exception as exc:
            logger.warning("ripgrep execution failed: %s", exc)
            return []

        if proc.returncode == 0 and stdout_bytes:
            results = _parse_ripgrep_output(stdout_bytes.decode("utf-8", errors="replace"), k=k)
            if results:
                logger.info(
                    "ripgrep found %d results for query=%r (whole_word=%s)",
                    len(results),
                    query[:100],
                    use_word,
                )
                return results

        # rc=1 means no matches — expected for rg
        if proc.returncode not in (0, 1):
            stderr = stderr_bytes.decode("utf-8", errors="replace")[:500] if stderr_bytes else ""
            logger.warning("ripgrep exit=%d: %s", proc.returncode, stderr)

        if use_word and proc.returncode == 1:
            logger.debug("No whole-word matches, retrying without -w")
            continue

    return []


def _resolve_search_roots() -> list[str]:
    """Return the list of directories to search, checking existence."""
    base = settings.base_dir.parent  # doctor/..  = project root
    roots: list[str] = []
    for rel in SEARCH_ROOTS:
        candidate = base / rel
        if candidate.is_dir():
            roots.append(str(candidate))
    # Always include demo-app if it exists as the primary target
    demo_app = base / "demo-app"
    if demo_app.is_dir() and str(demo_app) not in roots:
        roots.insert(0, str(demo_app))
    return roots


# ── ripgrep JSON parsing ───────────────────────────────────────────────


def _parse_ripgrep_output(raw: str, k: int) -> list[dict[str, Any]]:
    """Parse ``rg --json`` output into structured results.

    ripgrep ``--json`` emits one JSON object per line, each wrapped in a
    ``data`` envelope::

        {"type":"begin","data":{"path":{"text":"..."}}}
        {"type":"match","data":{"path":{"text":"..."},"lines":{"text":"..."},"line_number":25,...}}
        {"type":"context","data":{...}}
        {"type":"end","data":{...}}
        {"type":"summary","data":{...}}

    Message types:

    * ``begin``  — start of a file
    * ``match``  — a matching line
    * ``context`` — a context line (before or after a match)
    * ``end``    — end of a file (stats)
    * ``summary`` — overall summary

    We track per-file state and emit one result dict per match with its
    surrounding context lines attached.
    """
    results: list[dict[str, Any]] = []
    current_file_path: str | None = None
    pending_context: list[dict[str, Any]] = []  # context lines before next match
    saw_match_in_file: bool = False

    def _extract_path(obj: dict[str, Any]) -> str:
        """Extract file path from a ripgrep JSON object, normalizing to forward slashes."""
        raw: str = obj.get("data", {}).get("path", {}).get("text", "")
        return raw.replace("\\", "/")

    def _make_result(file_path: str, lineno: int, line_text: str) -> dict[str, Any]:
        return {
            "file_path": file_path,
            "line_number": lineno,
            "line_content": line_text.rstrip("\n\r"),
            "match_type": "ripgrep",
            "context_before": [],
            "context_after": [],
            "file_role": _classify_file_role(file_path),
        }

    for raw_line in raw.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue

        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        msg_type: str = obj.get("type", "")

        if msg_type == "begin":
            current_file_path = _extract_path(obj)
            pending_context = []
            saw_match_in_file = False

        elif msg_type == "match":
            path = _extract_path(obj) or current_file_path or ""
            lineno = obj.get("data", {}).get("line_number", 0)
            line_text = obj.get("data", {}).get("lines", {}).get("text", "")
            result = _make_result(path, lineno, line_text)

            # Attach previously buffered context (context-before)
            if pending_context:
                result["context_before"] = [
                    {"line_number": ctx["line_number"], "line_content": ctx["line_content"]}
                    for ctx in pending_context[-RG_CONTEXT_LINES:]
                ]
                pending_context.clear()

            results.append(result)
            saw_match_in_file = True
            if len(results) >= k:
                return results[:k]

        elif msg_type == "context":
            path = _extract_path(obj) or current_file_path or ""
            lineno = obj.get("data", {}).get("line_number", 0)
            line_text = obj.get("data", {}).get("lines", {}).get("text", "")
            ctx_entry = {
                "line_number": lineno,
                "line_content": line_text.rstrip("\n\r"),
                "file_path": path,
            }

            if saw_match_in_file and results:
                # Context-after: attach to the last emitted result
                last = results[-1]
                if last["file_path"] == path:
                    last["context_after"].append(
                        {
                            "line_number": lineno,
                            "line_content": line_text.rstrip("\n\r"),
                        }
                    )
                    max_after = RG_CONTEXT_LINES * 2
                    if len(last["context_after"]) > max_after:
                        last["context_after"] = last["context_after"][-max_after:]
            else:
                # Context-before: buffer for next match
                pending_context.append(ctx_entry)
                if len(pending_context) > RG_CONTEXT_LINES:
                    pending_context.pop(0)

        elif msg_type in ("end", "summary"):
            current_file_path = None
            pending_context = []
            saw_match_in_file = False

    return results[:k]


# ── Public API ─────────────────────────────────────────────────────────


def _build_fallback_suggestion(query: str) -> str:
    """Build a structured suggestion when ripgrep returns no matches.

    Instead of a phantom vector search that produces low-quality results,
    guide the Agent toward tools that actually work for diagnosis.
    """
    suggestion = {
        "match_type": "fallback",
        "results": [],
        "message": (f"ripgrep 未找到与 '{query[:120]}' 匹配的代码。建议下一步："),
        "suggestions": [
            {
                "action": "code_search",
                "hint": (
                    "尝试用更短的关键词重新搜索，如函数名、类名、"
                    "变量名的核心部分（去掉前后缀/下划线）"
                ),
            },
            {
                "action": "get_file_content",
                "hint": (
                    "如果已从 search_observability 的日志/Trace 中"
                    "获取到具体文件名和行号，直接用 get_file_content 打开"
                ),
            },
            {
                "action": "search_observability",
                "hint": (
                    "如果还没有拿到具体错误信息（stack trace / error log），"
                    "先调 search_observability 获取更精确的线索"
                ),
            },
        ],
    }
    return json.dumps(suggestion, ensure_ascii=False)


@traced()
async def code_search(query: str, k: int = 10) -> str:
    """Search the codebase with ripgrep — no vector fallback.

    When ripgrep returns nothing, the tool guides the Agent toward
    ``get_file_content`` or ``search_observability`` instead.

    Args:
        query: The search query.  Best for exact matches: function
               names, class names, variable names, SQL table names.
        k: Maximum number of results (1–20, default 10).

    Returns:
        JSON string: a list of result objects, each containing:
        ``file_path``, ``line_number``, ``line_content``, ``match_type``
        (``ripgrep``), ``context_before``, ``context_after``,
        ``file_role``.

        On no-match: a fallback JSON with ``match_type=fallback`` and
        ``suggestions`` for next steps.
    """
    k = min(max(1, k), 20)

    # ── Step 1: ripgrep exact/keyword match ────────────────────────
    logger.info("code_search_start query=%s k=%d", query[:200], k)
    try:
        rg_results = await _ripgrep_search(query, k=k)
    except Exception as exc:
        logger.warning("ripgrep search crashed: %s", exc)
        rg_results = []

    if rg_results:
        logger.info("code_search_rg_hit count=%d", len(rg_results))
        return json.dumps(rg_results, ensure_ascii=False)

    # ── Step 2: no match → structured fallback suggestion ──────────
    # Deliberately NO vector/RAG fallback — semantic search for code
    # identifiers is unreliable; guide the Agent toward better tools.
    logger.info("code_search_rg_miss query=%s → fallback suggestion", query[:200])
    return _build_fallback_suggestion(query)


# ── LangChain StructuredTool wrapper ────────────────────────────────────

CODE_SEARCH_TOOL = StructuredTool.from_function(
    coroutine=code_search,
    name="code_search",
    description=(
        "Search the demo-app codebase for exact code identifiers. "
        "Uses ripgrep for fast, precise matching. "
        "Best for: function names, class names, variable names, SQL table names. "
        "Not suitable for natural-language queries ('N+1 problem') — "
        "use search_observability first to get concrete identifiers, then retry. "
        "Examples: "
        "query='list_tasks' to find task listing function; "
        "query='TaskResponse' to find the Pydantic schema class. "
        "When no match: returns suggestions to try get_file_content or search_observability. "
        "Returns JSON with file_path, line_number, line_content, file_role, and context."
    ),
)
