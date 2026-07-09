"""
Frontend Specialist tools — dedicated tools for frontend bug diagnosis.

Provides LangChain StructuredTool instances for:
1. ``parse_browser_errors`` — Parse and structure browser_errors.json data
2. ``extract_stack_trace`` — Extract clean stack trace from error messages
3. (``source_map_resolve`` is in the shared tool pool)

These tools enable the Frontend Specialist to deeply analyze browser-side
errors (captured by Playwright/OTel-JS) and trace them back to original
TypeScript/JSX source locations.

Usage::

    from src.tools.frontend_tools import PARSE_BROWSER_ERRORS_TOOL, EXTRACT_STACK_TRACE_TOOL

    agent = create_agent(
        model=llm,
        tools=[..., PARSE_BROWSER_ERRORS_TOOL, EXTRACT_STACK_TRACE_TOOL],
    )
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.tools import StructuredTool

from src.observability.logger import get_logger
from src.observability.tracing import traced

logger = get_logger(__name__)

# ── Parse browser errors ─────────────────────────────────────────────


@traced()
async def parse_browser_errors(browser_errors_json: str) -> str:
    """
    Parse and structure a browser_errors.json payload into a detailed analysis.

    Extracts: error type, message, stack trace, source file, line number,
    trace_id, span_id, component stack, and breadcrumbs from each browser error.

    Also classifies errors:
    - ``TypeError`` / ``Cannot read properties of undefined`` → likely
      API contract mismatch (missing fields in response)
    - ``ReferenceError`` → missing variable or import
    - ``ChunkLoadError`` → lazy-loaded module failure
    - Promise rejection / unhandled rejection

    Args:
        browser_errors_json: Raw JSON string or file content of browser_errors.json.
            Can be a JSON array of browser error objects.

    Returns:
        JSON string with structured error analysis including:
        - ``total_errors``: count of errors
        - ``by_type``: errors grouped by JavaScript error type
        - ``cross_layer_hints``: indicators that the root cause may be backend-side
        - ``detailed_errors``: each error with parsed stack, file, line, component info
    """
    try:
        errors: list[dict[str, Any]] = (
            json.loads(browser_errors_json)
            if isinstance(browser_errors_json, str)
            else browser_errors_json
        )

        if not isinstance(errors, list):
            return json.dumps(
                {"error": "Expected a JSON array of browser errors", "total_errors": 0},
                ensure_ascii=False,
            )

        if not errors:
            return json.dumps(
                {
                    "total_errors": 0,
                    "message": "No browser errors found.",
                    "by_type": {},
                    "cross_layer_hints": [],
                    "detailed_errors": [],
                },
                ensure_ascii=False,
            )

        by_type: dict[str, list[dict[str, Any]]] = {}
        cross_layer_hints: list[str] = []
        detailed: list[dict[str, Any]] = []

        for err in errors:
            message = str(err.get("message", ""))
            stack = str(err.get("stack", ""))
            component_stack = str(err.get("component_stack", ""))
            trace_id = str(err.get("trace_id", ""))
            span_id = str(err.get("span_id", ""))

            # Classify error type
            error_type = _classify_error_type(message, stack)

            # Extract file and line from stack
            file_info = _extract_file_line_from_stack(stack or message)

            detail = {
                "error_type": error_type,
                "message": message[:500],
                "source_file": file_info.get("file", "unknown"),
                "source_line": file_info.get("line", 0),
                "source_column": file_info.get("column", 0),
                "component": _extract_component_name(component_stack or message),
                "trace_id": trace_id,
                "span_id": span_id,
                "has_react_stack": "react" in (stack + component_stack + message).lower(),
                "timestamp": str(err.get("timestamp", "")),
            }

            detailed.append(detail)
            by_type.setdefault(error_type, []).append(detail)

            # Cross-layer heuristics
            if (
                error_type.startswith("TypeError")
                and ("undefined" in message.lower() or "null" in message.lower())
                and ("reading" in message.lower() or "properties" in message.lower())
            ):
                hint = (
                    f"⚠️ 跨层嫌疑：前端读取 undefined 字段 → "
                    f"检查 trace_id={trace_id} 对应的 API 响应是否缺字段"
                )
                cross_layer_hints.append(hint)

            msg_lower = message.lower()
            if (
                "api" in msg_lower or "fetch" in msg_lower or "response" in msg_lower
            ) and error_type != "NetworkError":
                cross_layer_hints.append(f"⚠️ 可能涉及 API 调用问题：{message[:200]}")

        result = {
            "total_errors": len(errors),
            "by_type": {k: len(v) for k, v in by_type.items()},
            "cross_layer_hints": cross_layer_hints[:10],
            "detailed_errors": detailed[:20],  # cap at 20
        }

        logger.info(
            "parse_browser_errors_completed",
            total=len(errors),
            types=list(by_type.keys()),
            cross_layer_hints=len(cross_layer_hints),
        )

        return json.dumps(result, ensure_ascii=False, indent=2)

    except json.JSONDecodeError as exc:
        logger.error("parse_browser_errors_invalid_json", error=str(exc))
        return json.dumps(
            {"error": f"Invalid JSON: {exc}", "total_errors": 0},
            ensure_ascii=False,
        )
    except Exception as exc:
        logger.error("parse_browser_errors_failed", error=str(exc))
        return json.dumps(
            {"error": str(exc), "total_errors": 0},
            ensure_ascii=False,
        )


def _classify_error_type(message: str, stack: str) -> str:
    """Classify a browser error by its message and stack content."""
    combined = (message + " " + stack).lower()

    if "typeerror" in combined:
        if "undefined" in combined:
            return "TypeError(undefined_access)"
        if "null" in combined:
            return "TypeError(null_access)"
        if "not a function" in combined:
            return "TypeError(not_a_function)"
        return "TypeError"

    if "referenceerror" in combined:
        return "ReferenceError"
    if "syntaxerror" in combined:
        return "SyntaxError"
    if "rangeerror" in combined:
        return "RangeError"
    if "networkerror" in combined or "failed to fetch" in combined:
        return "NetworkError"
    if "chunkloaderror" in combined or "loading chunk" in combined:
        return "ChunkLoadError"
    if "unhandled rejection" in combined or "promise" in combined:
        return "PromiseRejection"
    if "react" in combined and "error" in combined:
        return "ReactRenderError"

    return "UnknownError"


def _extract_file_line_from_stack(stack_text: str) -> dict[str, Any]:
    """Extract source file, line, and column from a stack trace string."""
    # Match patterns like:
    #   at SortableTaskCard (http://localhost:5173/src/pages/TaskBoardPage.tsx:148:17)
    #   at http://localhost:5173/src/pages/TaskBoardPage.tsx?t=178:148:17
    pattern = (
        r"(?:at\s+)?(?:\(?(?:https?://[^/]+)?(/[^:?\s)]+\.(?:tsx?|jsx?|js|ts))[^:]*:(\d+):(\d+)\)?)"
    )
    match = re.search(pattern, stack_text)
    if match:
        return {
            "file": match.group(1),
            "line": int(match.group(2)),
            "column": int(match.group(3)),
        }
    return {"file": "", "line": 0, "column": 0}


def _extract_component_name(text: str) -> str:
    """Extract React component name from error message or component stack."""
    # Pattern: "at ComponentName (http://...)"
    pattern = r"at\s+(\w+)\s*\("
    match = re.search(pattern, text)
    if match:
        return match.group(1)
    # Fallback: look for component_stack field
    pattern2 = r"at\s+(\w+)\s+\(http"
    match2 = re.search(pattern2, text)
    if match2:
        return match2.group(1)
    return "unknown"


# ── Extract stack trace ──────────────────────────────────────────────


@traced()
async def extract_stack_trace(error_msg: str) -> str:
    """
    Extract and clean a JavaScript/React stack trace from an error message.

    Normalizes paths, removes noise (node_modules, vite deps boilerplate),
    and highlights the most relevant frames — especially user-authored
    source files (``src/**/*.tsx``, ``src/**/*.ts``).

    Also extracts:
    - The React component where the error originated
    - The line number in the source file
    - Whether the error is a render error, event handler error, or Promise rejection

    Args:
        error_msg: The raw error message or stack trace string.

    Returns:
        JSON string with:
        - ``cleaned_stack``: list of cleaned stack frames
        - ``user_frames``: frames in user-authored source files (most relevant)
        - ``component_name``: inferred React component
        - ``error_type``: classification of the error
        - ``source_location``: {file, line, column} of the top user frame
    """
    try:
        if not error_msg:
            return json.dumps(
                {"error": "Empty error message", "cleaned_stack": []},
                ensure_ascii=False,
            )

        # Split into lines
        lines = error_msg.split("\n")

        # Classify error from first line
        error_type = _classify_error_type(lines[0] if lines else "", error_msg)

        cleaned_frames: list[dict[str, Any]] = []
        user_frames: list[dict[str, Any]] = []

        stack_line_pattern = re.compile(r"at\s+(.+?)\s*\(?(https?://[^)]+|/[^:?\s)]+\.\w+)[^)]*\)?")

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Skip noise lines
            if any(
                skip in line
                for skip in [
                    "node_modules/.vite/deps/react-dom",
                    "node_modules/.vite/deps/react.",
                    "node_modules/.vite/deps/chunk-",
                    "node_modules/.pnpm/",
                    "vite/dist/",
                    "@vite/client",
                    "console.error",
                    "[CLIENT_ERROR]",
                ]
            ):
                continue

            match = stack_line_pattern.search(line)
            if match:
                func = match.group(1).strip()
                path = match.group(2).strip()

                # Extract line:column from path
                line_col_match = re.search(r":(\d+):(\d+)$", path)
                line_no = int(line_col_match.group(1)) if line_col_match else 0
                col_no = int(line_col_match.group(2)) if line_col_match else 0
                clean_path = re.sub(r":\d+:\d+$", "", path)

                frame = {
                    "function": func,
                    "file": clean_path,
                    "line": line_no,
                    "column": col_no,
                    "is_user_code": any(p in clean_path for p in ["src/", "app/"]),
                }
                cleaned_frames.append(frame)
                if frame["is_user_code"]:
                    user_frames.append(frame)

        # Extract component name
        component_name = _extract_component_name(error_msg)

        # Top user frame is the most relevant
        top_user_frame = (
            user_frames[0]
            if user_frames
            else (cleaned_frames[0] if cleaned_frames else {"file": "", "line": 0, "column": 0})
        )

        result = {
            "error_type": error_type,
            "component_name": component_name,
            "source_location": {
                "file": top_user_frame.get("file", ""),
                "line": top_user_frame.get("line", 0),
                "column": top_user_frame.get("column", 0),
            },
            "user_frames": user_frames[:10],
            "cleaned_stack": cleaned_frames[:20],
            "total_frames": len(cleaned_frames),
            "user_frames_count": len(user_frames),
        }

        logger.info(
            "extract_stack_trace_completed",
            error_type=error_type,
            component=component_name,
            user_frames=len(user_frames),
        )

        return json.dumps(result, ensure_ascii=False, indent=2)

    except Exception as exc:
        logger.error("extract_stack_trace_failed", error=str(exc))
        return json.dumps(
            {"error": str(exc), "cleaned_stack": []},
            ensure_ascii=False,
        )


# ── LangChain StructuredTool wrappers ─────────────────────────────────

PARSE_BROWSER_ERRORS_TOOL = StructuredTool.from_function(
    coroutine=parse_browser_errors,
    name="parse_browser_errors",
    description=(
        "Parse and analyze browser error data (browser_errors.json). "
        "Extracts error types (TypeError, ReferenceError, etc.), stack traces, "
        "source files, line numbers, React component names, trace_id/span_id "
        "links, and **cross-layer hints** (e.g. undefined field access that may "
        "indicate a backend API response missing required fields). "
        "Input: the raw JSON content of browser_errors.json as a string. "
        "Use this FIRST when diagnosing frontend crashes — it gives you a "
        "structured overview before diving into stack traces."
    ),
)

EXTRACT_STACK_TRACE_TOOL = StructuredTool.from_function(
    coroutine=extract_stack_trace,
    name="extract_stack_trace",
    description=(
        "Extract and clean a JavaScript/React stack trace from an error message. "
        "Normalizes file paths, removes framework noise (react-dom, vite), and "
        "highlights user-authored source files (src/**/*.tsx, src/**/*.ts). "
        "Returns the cleaned stack frames, the React component where the error "
        "occurred, the source file and line number, and error classification. "
        "Use this after parse_browser_errors to get precise file:line locations "
        "for code_search or source_map_resolve."
    ),
)

# Public exports for specialist agents
FRONTEND_SPECIALIST_TOOLS: list[StructuredTool] = [
    PARSE_BROWSER_ERRORS_TOOL,
    EXTRACT_STACK_TRACE_TOOL,
]

__all__ = [
    "parse_browser_errors",
    "extract_stack_trace",
    "PARSE_BROWSER_ERRORS_TOOL",
    "EXTRACT_STACK_TRACE_TOOL",
    "FRONTEND_SPECIALIST_TOOLS",
]
