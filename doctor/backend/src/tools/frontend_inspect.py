"""
一站式前端错误分析工具 (V3)。

合并 ``parse_browser_errors`` + ``extract_stack_trace`` + ``source_map_resolve``
为一个统一入口 ``inspect_frontend_error``。

设计原则:
- 输入 browser_errors JSON 字符串，输出结构化分析
- 自动分类错误类型（TypeError(undefined_access) 等）
- 自动检测跨层根因（如 undefined 读取 → 可能后端缺字段）
- resolve_sourcemap=True 时还原每个栈帧到源码位置

Usage:
    from src.tools.frontend_inspect import inspect_frontend_error

    result = await inspect_frontend_error(
        browser_errors='[{"message": "Cannot read properties of undefined", ...}]',
        resolve_sourcemap=True,
    )
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.observability.logger import get_logger
from src.observability.tracing import traced
from src.tools.frontend_tools import _classify_error_type, _extract_component_name
from src.tools.source_map_resolve import source_map_resolve

logger = get_logger(__name__)

# ── Constants ───────────────────────────────────────────────────────

# Patterns that indicate a cross-layer (frontend symptom, backend root cause) issue
_CROSS_LAYER_UNDEFINED_RE = re.compile(r"cannot read propert(?:y|ies) of undefined", re.IGNORECASE)
_CROSS_LAYER_NULL_RE = re.compile(r"cannot read propert(?:y|ies) of null", re.IGNORECASE)
_CROSS_LAYER_API_RE = re.compile(
    r"(api|fetch|response|network|status\s*5\d\d|status\s*4\d\d)", re.IGNORECASE
)

# Maximum number of stack frames to resolve via source map
MAX_SOURCEMAP_RESOLVE_FRAMES: int = 5


# ── Helper: extract file/line from browser error ────────────────────


def _extract_file_line_from_message(message: str) -> dict[str, Any]:
    """Extract source file, line, and column from an error message.

    Matches patterns like:
        at SortableTaskCard (http://localhost:5173/src/pages/TaskBoardPage.tsx:148:17)
    """
    pattern = re.compile(
        r"\(?(?:https?://[^/]+)?(/[^:?\s)]+\.(?:tsx?|jsx?|js|ts))[^:]*:(\d+):(\d+)\)?"
    )
    match = pattern.search(message)
    if match:
        return {
            "file": match.group(1),
            "line": int(match.group(2)),
            "column": int(match.group(3)),
        }
    return {"file": "", "line": 0, "column": 0}


def _extract_stack_frames(message: str) -> list[dict[str, Any]]:
    """Extract all stack frames from an error message string.

    Returns a list of {file, line, column, function_name, component} dicts.
    Only includes user-authored source files (src/ paths), filtering out
    node_modules and framework internals.
    """
    frames: list[dict[str, Any]] = []

    # Match lines like: at FuncName (http://host:port/path?query:line:col)
    # Need to handle port numbers in the URL (e.g. localhost:5173)
    frame_pattern = re.compile(
        r"at\s+(\S+?)\s*\(?(?:https?://[^/\s]+)?"
        r"(/[^:?\s)]+\.(?:tsx?|jsx?|js|ts))"
        r"[^:]*:(\d+):(\d+)\)?"
    )

    for match in frame_pattern.finditer(message):
        func_name = match.group(1).strip() if match.group(1) else ""
        file_path = match.group(2) if match.group(2) else ""
        line_no = int(match.group(3)) if match.group(3) else 0
        col_no = int(match.group(4)) if match.group(4) else 0

        # Skip framework/infra noise
        if any(
            skip in file_path
            for skip in [
                "node_modules",
                "vite/",
                "@vite",
                "react-dom",
                "react.",
                "chunk-",
                ".pnpm/",
            ]
        ):
            continue

        # Skip non-user-code paths
        if not any(prefix in file_path for prefix in ["src/", "app/", "pages/", "components/"]):
            continue

        frames.append(
            {
                "file": file_path,
                "line": line_no,
                "column": col_no,
                "function_name": func_name,
                "component": _extract_component_name(
                    f"at {func_name} ({file_path}:{line_no}:{col_no})"
                ),
            }
        )

    return frames


# ── Helper: generate cross-layer hints ───────────────────────────────


def _generate_cross_layer_hint(
    error_type: str,
    message: str,
    trace_id: str,
) -> str | None:
    """Generate a cross-layer diagnostic hint based on error patterns.

    Returns a hint string if a cross-layer issue is suspected, otherwise None.
    """
    msg_lower = message.lower()

    # Pattern 1: Cannot read properties of undefined → likely API missing fields
    if _CROSS_LAYER_UNDEFINED_RE.search(msg_lower):
        hint = "该错误是读取 undefined 属性，建议检查后端 API 响应是否缺字段。"
        if trace_id:
            hint += f" 可通过 trace_id={trace_id} 在 Tempo 中查看对应的后端请求/响应。"
        hint += (
            " 常见原因：1) 后端返回的 JSON 缺少该字段；"
            "2) API 返回了错误格式（如 HTML 而非 JSON）；"
            "3) 前端在数据加载完成前就渲染了组件。"
        )
        return hint

    # Pattern 2: Cannot read properties of null → null response
    if _CROSS_LAYER_NULL_RE.search(msg_lower):
        hint = "该错误是读取 null 属性，可能是后端返回了 null 值或 API 调用失败返回空。"
        if trace_id:
            hint += f" 可通过 trace_id={trace_id} 排查。"
        return hint

    # Pattern 3: API/fetch/response mentioned
    if _CROSS_LAYER_API_RE.search(msg_lower) and error_type != "NetworkError":
        return "错误消息中提到了 API/网络相关关键词，可能是后端响应异常导致前端报错。"

    return None


# ── Public API ──────────────────────────────────────────────────────


@traced("frontend.inspect_frontend_error")
async def inspect_frontend_error(
    browser_errors: str,
    resolve_sourcemap: bool = True,
) -> str:
    """一站式前端错误分析。

    合并 parse_browser_errors + extract_stack_trace + source_map_resolve，
    输入 browser_errors JSON，输出结构化分析结果。

    返回 JSON 结构:
        {
          "errors": [{
            "type": "TypeError(undefined_access)",
            "message": "...",
            "stack_frames": [{"file": "...", "line": 123, "component": "TaskBoard"}],
            "cross_layer_hint": "..."
          }],
          "summary": "共 N 个前端错误，其中 M 个疑似跨层根因"
        }

    Args:
        browser_errors: browser_errors.json 的 JSON 字符串内容。
        resolve_sourcemap: 是否通过 source map 还原每个栈帧（默认 True）。

    Returns:
        JSON 字符串，包含 errors 数组和 summary。
    """
    try:
        errors: list[dict[str, Any]] = json.loads(browser_errors)
    except json.JSONDecodeError as exc:
        logger.error("inspect_frontend_error_invalid_json", error=str(exc))
        return json.dumps(
            {
                "errors": [],
                "summary": f"输入 JSON 解析失败: {exc}",
                "parse_error": True,
            },
            ensure_ascii=False,
        )

    if not isinstance(errors, list):
        return json.dumps(
            {
                "errors": [],
                "summary": "输入应为 JSON 数组",
                "parse_error": True,
            },
            ensure_ascii=False,
        )

    if not errors:
        return json.dumps(
            {
                "errors": [],
                "summary": "无浏览器错误",
            },
            ensure_ascii=False,
        )

    analyzed_errors: list[dict[str, Any]] = []
    cross_layer_count: int = 0

    for err in errors:
        message = str(err.get("message", ""))
        stack = str(err.get("stack", "") if err.get("stack") else "")
        component_stack = str(err.get("component_stack", ""))
        trace_id = str(err.get("trace_id", ""))
        span_id = str(err.get("span_id", ""))
        timestamp = str(err.get("timestamp", ""))

        # Combine all text for analysis
        full_text = f"{message}\n{stack}\n{component_stack}"

        # Classify error type
        error_type = _classify_error_type(message, stack or component_stack)

        # Extract component name
        component = _extract_component_name(full_text)
        if component == "unknown" and component_stack:
            component = _extract_component_name(component_stack)

        # Extract stack frames
        stack_frames = _extract_stack_frames(full_text)

        # If no frames found by regex, try file_line extraction as fallback
        if not stack_frames:
            fl = _extract_file_line_from_message(full_text)
            if fl["file"]:
                stack_frames = [
                    {
                        "file": fl["file"],
                        "line": fl["line"],
                        "column": fl["column"],
                        "function_name": "",
                        "component": component,
                    }
                ]

        # Resolve sourcemaps for top frames
        if resolve_sourcemap and stack_frames:
            for frame in stack_frames[:MAX_SOURCEMAP_RESOLVE_FRAMES]:
                try:
                    sm_result = json.loads(await source_map_resolve(frame["file"], frame["line"]))
                    if sm_result.get("original_file"):
                        frame["original_file"] = sm_result["original_file"]
                        frame["original_line"] = sm_result.get("original_line", frame["line"])
                        frame["original_column"] = sm_result.get("original_column", 0)
                        frame["original_name"] = sm_result.get("original_name", "")
                except Exception:
                    # Source map resolve failed for this frame — leave as-is
                    pass

        # Generate cross-layer hint
        cross_layer_hint = _generate_cross_layer_hint(error_type, message, trace_id)
        if cross_layer_hint:
            cross_layer_count += 1

        analyzed = {
            "type": error_type,
            "message": message[:500],  # Truncate long messages
            "component": component,
            "trace_id": trace_id,
            "span_id": span_id,
            "timestamp": timestamp,
            "stack_frames": stack_frames[:10],  # Cap at 10 frames
        }
        if cross_layer_hint:
            analyzed["cross_layer_hint"] = cross_layer_hint

        analyzed_errors.append(analyzed)

    # ── Build summary ──
    total = len(analyzed_errors)
    summary_parts = [f"共 {total} 个前端错误"]
    if cross_layer_count > 0:
        summary_parts.append(f"其中 {cross_layer_count} 个疑似跨层根因")
    else:
        summary_parts.append("未检测到跨层关联")

    # Add type distribution
    type_counts: dict[str, int] = {}
    for e in analyzed_errors:
        t = e["type"]
        type_counts[t] = type_counts.get(t, 0) + 1
    if type_counts:
        type_summary = "、".join(f"{t}({c}个)" for t, c in sorted(type_counts.items()))
        summary_parts.append(f"类型分布: {type_summary}")

    summary = "，".join(summary_parts)

    result = {
        "errors": analyzed_errors,
        "summary": summary,
        "total": total,
        "cross_layer_count": cross_layer_count,
    }

    logger.info(
        "inspect_frontend_error_completed",
        total=total,
        cross_layer_count=cross_layer_count,
        types=list(type_counts.keys()),
    )

    return json.dumps(result, ensure_ascii=False, indent=2)


# ── LangChain StructuredTool ─────────────────────────────────────────


def _build_inspect_frontend_error_tool() -> Any:
    """Build the LangChain StructuredTool for inspect_frontend_error."""
    from langchain_core.tools import StructuredTool

    return StructuredTool.from_function(
        coroutine=inspect_frontend_error,
        name="inspect_frontend_error",
        description=(
            "一站式前端错误分析工具。\n"
            "输入 browser_errors.json 的 JSON 字符串，自动完成：\n"
            "1. 错误类型分类（TypeError(undefined_access)、PromiseRejection 等）\n"
            "2. 栈帧提取（过滤框架噪声，保留用户源码位置）\n"
            "3. 跨层根因检测（如 undefined 读取 → 可能后端 API 缺字段）\n"
            "4. Source map 还原（resolve_sourcemap=True 时自动还原到源码）\n"
            "返回 JSON: {errors: [...], summary: '...'}\n"
            "每个 error 含 type、message、stack_frames、component、trace_id、cross_layer_hint"
        ),
    )


# Deferred construction: cached on first access
_inspect_tool_cache: Any = None


def get_inspect_frontend_error_tool() -> Any:
    """Get or create the cached INSPECT_FRONTEND_ERROR_TOOL."""
    global _inspect_tool_cache
    if _inspect_tool_cache is None:
        _inspect_tool_cache = _build_inspect_frontend_error_tool()
    return _inspect_tool_cache


# ── Public API ──────────────────────────────────────────────────────

__all__ = [
    "inspect_frontend_error",
    "get_inspect_frontend_error_tool",
]
