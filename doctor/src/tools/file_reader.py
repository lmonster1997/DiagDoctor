"""
文件读取工具 (V3)。

为诊断 Agent 提供读取 demo-app 代码库中指定文件的能力。
支持行范围截取、路径安全校验、大文件截断。

Usage:
    from src.tools.file_reader import get_file_content

    content = await get_file_content("app/services/task_service.py", start_line=40, end_line=60)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.config import settings
from src.observability.logger import get_logger
from src.observability.tracing import traced

logger = get_logger(__name__)

# ── Constants ───────────────────────────────────────────────────────

MAX_LINES: int = 200
MAX_FILE_SIZE_BYTES: int = 500_000  # 500KB — refuse to read binary/large files


# ── Path resolution ─────────────────────────────────────────────────


def _resolve_demo_app_root() -> Path:
    """Resolve the absolute path to the demo-app repository root.

    Computed relative to the doctor project's base_dir:
        <project_root>/demo-app/
    """
    base = settings.base_dir  # doctor/
    demo_root = (base.parent / "demo-app").resolve()
    return demo_root


def _resolve_path(file_path: str) -> Path:
    """Resolve a relative file_path to an absolute path within demo-app.

    Args:
        file_path: Relative path from demo-app root (e.g. "app/services/task_service.py").

    Returns:
        Resolved absolute Path.

    Raises:
        ValueError: If the path escapes the demo-app directory.
    """
    demo_root = _resolve_demo_app_root()

    # Explicit check: reject paths with ".." segments BEFORE any normalization
    raw_normalized = file_path.replace("\\", "/")
    if ".." in raw_normalized.split("/"):
        raise ValueError(
            f"路径越界：'{file_path}' 包含 '..' 目录遍历，不允许访问 demo-app 之外的路径。"
        )

    # Normalise: strip leading ./ prefix, then leading /
    clean = raw_normalized
    while clean.startswith("./"):
        clean = clean[2:]
    clean = clean.lstrip("/")

    # Resolve relative to demo-app root
    resolved = (demo_root / clean).resolve()

    # Security: ensure the resolved path is within demo-app
    try:
        resolved.relative_to(demo_root)
    except ValueError as err:
        raise ValueError(
            f"路径越界：'{file_path}' 不在 demo-app 仓库范围内。 demo-app 根目录为 {demo_root}"
        ) from err

    return resolved


# ── Public API ──────────────────────────────────────────────────────


@traced("file_reader.get_file_content")
async def get_file_content(
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """读取 demo-app 代码库中的指定文件。

    路径相对于 demo-app 仓库根目录，例如：
    - ``"app/services/task_service.py"`` → backend 代码
    - ``"app/models/task.py"`` → 数据模型
    - ``"src/pages/TaskBoardPage.tsx"`` → 前端代码

    支持行范围截取，不传 start_line/end_line 则返回整个文件（最大 {MAX_LINES} 行）。

    Args:
        file_path: 相对于 demo-app 根目录的文件路径。
        start_line: 起始行号（1-based，含）。None 表示从第 1 行开始。
        end_line: 结束行号（1-based，含）。None 表示到文件末尾。

    Returns:
        文件内容字符串，带行号前缀。格式：
        ```
        [文件] app/services/task_service.py (第 40-60 行 / 共 200 行)
         40| def get_task_by_id(task_id: str) -> Task:
         41|     ...
        ```

        如果文件过大被截断，末尾会附加截断提示。
    """
    # ── Resolve and validate path ──
    try:
        resolved = _resolve_path(file_path)
    except ValueError as exc:
        logger.warning("get_file_content_path_rejected", file_path=file_path, error=str(exc))
        return str(exc)

    # ── Check file exists ──
    if not resolved.exists():
        logger.warning("get_file_content_not_found", file_path=file_path, resolved=str(resolved))
        return (
            f"文件不存在：'{file_path}'\n"
            f"完整路径：{resolved}\n"
            f"提示：请确认文件路径相对于 demo-app 根目录，例如 'app/services/task_service.py'"
        )

    if not resolved.is_file():
        logger.warning("get_file_content_not_a_file", file_path=file_path, resolved=str(resolved))
        return f"路径不是文件：'{file_path}'（可能是目录）"

    # ── Size check ──
    try:
        file_size = resolved.stat().st_size
    except OSError:
        file_size = 0

    if file_size > MAX_FILE_SIZE_BYTES:
        logger.warning(
            "get_file_content_too_large",
            file_path=file_path,
            size=file_size,
            max=MAX_FILE_SIZE_BYTES,
        )
        return (
            f"文件过大（{file_size / 1024:.0f} KB），"
            f"超过 {MAX_FILE_SIZE_BYTES / 1024:.0f} KB 上限。"
            f" 请缩小范围使用 start_line/end_line 参数。"
        )

    # ── Read file ──
    try:
        raw_content = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        logger.warning("get_file_content_binary", file_path=file_path)
        return f"无法读取 '{file_path}'：文件可能是二进制格式，不支持文本读取。"
    except OSError as exc:
        logger.error("get_file_content_read_error", file_path=file_path, error=str(exc))
        return f"读取文件失败：{exc}"

    lines = raw_content.split("\n")
    total_lines = len(lines)

    # ── Apply line range ──
    sl = max(1, start_line) if start_line is not None else 1
    el = min(total_lines, end_line) if end_line is not None else total_lines

    if sl > total_lines:
        return f"行号超出范围：start_line={sl}，但文件只有 {total_lines} 行。\n文件：{file_path}"

    if sl > el:
        return f"start_line ({sl}) 不能大于 end_line ({el})"

    selected_lines = lines[sl - 1 : el]
    selected_count = len(selected_lines)

    # ── Truncate if too many lines ──
    truncated = False
    if selected_count > MAX_LINES:
        selected_lines = selected_lines[:MAX_LINES]
        truncated = True
        logger.warning(
            "get_file_content_truncated",
            file_path=file_path,
            requested=selected_count,
            max=MAX_LINES,
        )

    # ── Format output ──
    range_desc = f"第 {sl}-{el} 行" if start_line or end_line else f"全部 {total_lines} 行"
    header = f"[文件] {file_path} ({range_desc} / 共 {total_lines} 行)\n"

    numbered = []
    for i, line in enumerate(selected_lines):
        line_num = sl + i
        numbered.append(f"{line_num:>5d}| {line}")

    result = header + "\n".join(numbered)

    if truncated:
        result += (
            f"\n\n... [截断] 仅显示前 {MAX_LINES} 行，"
            f"实际选中 {selected_count} 行。"
            f" 使用 start_line/end_line 缩小范围。"
        )

    logger.info(
        "get_file_content_completed",
        file_path=file_path,
        total_lines=total_lines,
        selected_lines=selected_count,
        truncated=truncated,
    )

    return result


# ── LangChain StructuredTool ─────────────────────────────────────────


def _build_get_file_content_tool() -> Any:
    """Build the LangChain StructuredTool for get_file_content."""
    from langchain_core.tools import StructuredTool

    return StructuredTool.from_function(
        coroutine=get_file_content,
        name="get_file_content",
        description=(
            "读取 demo-app 代码库中的指定文件内容。\n"
            "file_path 相对于 demo-app 根目录，如 'app/services/task_service.py' 或 "
            "'src/pages/TaskBoardPage.tsx'。\n"
            "可选 start_line 和 end_line（1-based）截取行范围。\n"
            "最大返回 200 行，超过自动截断。\n"
            "用于：Agent 需要查看特定源码文件内容以验证根因假设时。"
        ),
    )


# Deferred construction: cached on first access
_file_tool_cache: Any = None


def get_get_file_content_tool() -> Any:
    """Get or create the cached GET_FILE_CONTENT_TOOL."""
    global _file_tool_cache
    if _file_tool_cache is None:
        _file_tool_cache = _build_get_file_content_tool()
    return _file_tool_cache


# ── Public API ──────────────────────────────────────────────────────

__all__ = [
    "get_file_content",
    "get_get_file_content_tool",
]
