"""
工具结果截断 — 入 context 前的预算控制。

提供 ``truncate_tool_result()`` 按工具类型的字符上限截断结果，
优先保留含 error/exception/trace 等关键词的关键行。
"""

from __future__ import annotations

import re

from src.config import settings

# 各工具类型的字符上限（按 ~4 chars/token 估算）
TOOL_CHAR_LIMITS: dict[str, int] = {
    "search_observability": 6_000,
    "code_search": 4_000,
    "get_file_content": 8_000,
    "db_query": 3_200,
    "inspect_frontend_error": 4_000,
}

_DEFAULT_CHAR_LIMIT = 4_000

# 关键行关键词（匹配这些词的行优先保留）
_KEY_LINE_PATTERNS: list[str] = [
    "error",
    "exception",
    "trace",
    "span",
    "fail",
    "line",
    "warning",
    "critical",
    "fatal",
    "crash",
    "panic",
    "timeout",
    "refused",
    "denied",
    "forbidden",
    "stack",
    "at ",
    " caused by",
    "root cause",
    "500",
    "502",
    "503",
    "504",
    "4xx",
    "5xx",
]

_KEY_LINE_RE = re.compile("|".join(_KEY_LINE_PATTERNS), re.IGNORECASE)

_HEAD_LINES = 15
_TAIL_LINES = 10
_COMPRESS_MARKER = "[已压缩]"


def truncate_tool_result(tool_name: str, content: str) -> str:
    """工具结果入 context 前的预算控制。

    策略:
    1. 按工具类型设字符上限
    2. 超上限时优先保留关键行（含 error/exception/trace/span 等关键词）
    3. 关键行不足时保留头尾（前 15 + 后 10 行），中间省略
    4. 追加 ``[已压缩]`` 标记
    """
    if not settings.tool_result_truncation_enabled:
        return content

    char_limit = TOOL_CHAR_LIMITS.get(tool_name, _DEFAULT_CHAR_LIMIT)

    if len(content) <= char_limit:
        return content

    lines = content.split("\n")
    total_lines = len(lines)

    # ── 策略 1：仅保留关键行 ──
    key_lines: list[str] = []
    key_line_indices: set[int] = set()
    for i, line in enumerate(lines):
        if _KEY_LINE_RE.search(line):
            key_lines.append(line)
            key_line_indices.add(i)

    key_content = "\n".join(key_lines)
    if len(key_content) <= char_limit:
        enriched: list[str] = []
        for i in range(total_lines):
            if any(abs(i - ki) <= 1 for ki in key_line_indices):
                enriched.append(lines[i])
        enriched_content = "\n".join(enriched)
        if len(enriched_content) <= char_limit:
            return enriched_content + f"\n{_COMPRESS_MARKER}（保留 {len(key_lines)} 个关键事件）"
        return key_content + f"\n{_COMPRESS_MARKER}"

    # ── 策略 2：关键行不足 → 保留头尾 ──
    compress_marker_full = (
        f"\n... [省略中间 {total_lines - _HEAD_LINES - _TAIL_LINES} 行] ...\n{_COMPRESS_MARKER}"
    )
    marker_len = len(compress_marker_full)
    available = char_limit - marker_len

    head_lines_count = min(_HEAD_LINES, total_lines)
    tail_lines_count = min(_TAIL_LINES, total_lines - head_lines_count)

    head_text = "\n".join(lines[:head_lines_count])
    tail_text = "\n".join(lines[-tail_lines_count:]) if tail_lines_count > 0 else ""

    while len(head_text) + len(tail_text) > available and (
        head_lines_count > 3 or tail_lines_count > 2
    ):
        if head_lines_count > tail_lines_count and head_lines_count > 3:
            head_lines_count -= 1
            head_text = "\n".join(lines[:head_lines_count])
        elif tail_lines_count > 2:
            tail_lines_count -= 1
            tail_text = "\n".join(lines[-tail_lines_count:])
        else:
            break

    omitted = total_lines - head_lines_count - tail_lines_count
    result = head_text + f"\n... [省略中间 {omitted} 行] ...\n" + _COMPRESS_MARKER
    if tail_lines_count > 0:
        result += "\n" + tail_text

    return result
