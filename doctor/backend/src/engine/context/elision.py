"""工具结果符号占位 - 运行时上下文管理(§7.1 / L2 可重取占位)。

与 ``truncation.py``(入口截断)正交:

- **truncation** 在工具结果**入 context 之前**裁剪(预防,规则驱动)。
- **elision** 在**运行时**把 N 轮前的旧 ToolMessage 替换成带重取入口的一行
  占位,保留"调过什么 + 关键结论 + 如何重取",丢原始大块 JSON。

L2 可重取占位(见 ``docs/L1-L4_Loki_Tempo_适用性分析.md``):DiagDoctor 的工具
结果可寻址、可无损重取(``search_observability`` 同参同结果,且返回已 echo
``query``/``time_range``),故占位可带重取句,agent 需要时一键重水合--比
Claude Code 的保守保信息更激进,但**不丢信息只换形式**。

占位字段来源对齐(§7.1 待对齐项的答案):重取入口取工具返回里已 echo 的
``query``/``time_range``(obs)或调用参数(其他工具);关键发现取
``insights``/``analysis.summary``(obs)或首条关键行(其他)。
"""

from __future__ import annotations

import json
from typing import Any

from src.observability.logger import get_logger

logger = get_logger(__name__)

# 占位"关键发现"最大长度(字符)
_FINDING_MAX = 300
# 占位"首条关键行"最大长度
_SNIPPET_MAX = 200
# 重取入口里单个参数值最大长度(防止超长 query/SQL 撑爆占位)
_ARG_MAX = 120


def build_elision_placeholder(
    tool_name: str, tool_call_args: dict[str, Any], result_content: str
) -> str:
    """构造带重取入口的符号占位,替换旧 ToolMessage 内容。

    Args:
        tool_name: 工具名(ToolMessage.name)。
        tool_call_args: 原始工具调用参数(从前一条 AIMessage 的 ``tool_calls``
            按 ``tool_call_id`` 匹配取)。重取入口的主要来源。
        result_content: 工具返回的原始内容(字符串)。用于提取关键发现。

    Returns:
        占位字符串:工具名 + 重取入口 + 关键发现 + 重取提示。
        任何解析失败都 fallback 到通用占位,**绝不抛错**(占位构造失败时保留
        原文比炸掉 agent 循环好)。
    """
    try:
        if tool_name == "search_observability":
            return _placeholder_search_observability(tool_call_args, result_content)
        if tool_name in ("code_search", "get_file_content"):
            return _placeholder_code_tool(tool_name, tool_call_args, result_content)
        if tool_name == "db_query":
            return _placeholder_db_query(tool_call_args, result_content)
        return _placeholder_generic(tool_name, tool_call_args, result_content)
    except Exception as exc:  # noqa: BLE001 - 占位构造绝不能炸
        logger.warning("elision_placeholder_failed", tool_name=tool_name, error=str(exc))
        return _placeholder_generic(tool_name, tool_call_args, result_content)


# ── Per-tool placeholder builders ────────────────────────────────────


def _placeholder_search_observability(args: dict[str, Any], content: str) -> str:
    """obs 占位:重取入口 = source+query+time_range;关键发现 = insights/summary。

    ``search_observability`` 返回 JSON 已 echo ``source``/``query``/``time_range``
    (``observability_unified.py:1227``)+ ``insights``/``analysis.summary``。
    若 JSON 被 ToolTruncation 的 head/tail 截断破坏(§7.6 worst case),退回用
    调用参数 ``args`` 构造重取入口--参数仍能重取,不阻塞。
    """
    data = _safe_json(content)
    if data is not None:
        source = data.get("source", args.get("source", "auto"))
        query = data.get("query", args.get("query", ""))
        tr = data.get("time_range") or {}
        start = tr.get("start", args.get("start", ""))
        end = tr.get("end", args.get("end", ""))
        finding = _extract_obs_finding(data)
    else:
        # JSON 不可解析(被行级截断破坏) -> 用调用参数构造重取入口
        source = args.get("source", "auto")
        query = args.get("query", "")
        start = args.get("start", "")
        end = args.get("end", "")
        finding = _first_key_line(content)

    handle = (
        f'search_observability(source="{_clip(source, 40)}", '
        f'query="{_clip(query, _ARG_MAX)}", start="{start}", end="{end}")'
    )
    return _format("search_observability", handle, finding)


def _placeholder_code_tool(name: str, args: dict[str, Any], content: str) -> str:
    """code_search / get_file_content 占位:重取入口 = query/file_path+行范围。"""
    if name == "code_search":
        handle = f'code_search(query="{_clip(args.get("query", ""), _ARG_MAX)}")'
    else:  # get_file_content
        handle = (
            f'get_file_content(file_path="{_clip(args.get("file_path", ""), _ARG_MAX)}", '
            f"start_line={args.get('start_line', '')}, end_line={args.get('end_line', '')})"
        )
    return _format(name, handle, _first_key_line(content))


def _placeholder_db_query(args: dict[str, Any], content: str) -> str:
    """db_query 占位:重取入口 = SQL(裁剪);结果首条关键行。"""
    handle = f'db_query(sql="{_clip(args.get("sql", ""), _ARG_MAX)}")'
    return _format("db_query", handle, _first_key_line(content))


def _placeholder_generic(
    name: str, args: dict[str, Any], content: str
) -> str:
    """通用占位:重取入口 = 工具名+参数;结果首条关键行。

    适用于 ``inspect_frontend_error`` / ``search_historical_root_cause`` / 未知工具。
    """
    arg_str = ", ".join(
        f'{k}="{_clip(str(v), 60)}"'
        for k, v in (args or {}).items()
        if k != "text"  # text 类长参数不入重取入口
    )
    handle = f"{name}({arg_str})" if arg_str else name
    return _format(name, handle, _first_key_line(content))


# ── Helpers ──────────────────────────────────────────────────────────


def _extract_obs_finding(data: dict[str, Any]) -> str:
    """从 obs 结果提取关键发现:insights > analysis.summary > 结构摘要 > 原始计数。"""
    insights = data.get("insights")
    if isinstance(insights, str) and insights.strip():
        return _clip(insights.strip(), _FINDING_MAX)

    analysis = data.get("analysis") or {}
    summary = analysis.get("summary")
    if isinstance(summary, str) and summary.strip():
        return _clip(summary.strip(), _FINDING_MAX)

    parts: list[str] = []
    if analysis.get("error_spans"):
        parts.append(f"error spans {len(analysis['error_spans'])}")
    if analysis.get("n_plus_one"):
        parts.append("检出 N+1")
    if analysis.get("bottlenecks"):
        parts.append(f"瓶颈 {len(analysis['bottlenecks'])} 处")
    if analysis.get("span_count"):
        parts.append(f"span {analysis['span_count']}")
    if parts:
        return ", ".join(parts)

    # 结构化截断后的原始计数(§7.6 keep-all slim 后仍巨大时的折叠标记)
    oc = data.get("_original_counts")
    if isinstance(oc, dict) and oc:
        return "原始结果较大已折叠: " + ", ".join(f"{k}={v}" for k, v in oc.items())

    return "(无可提取结论)"


def _format(name: str, handle: str, finding: str) -> str:
    """统一占位格式:工具名(隐于 handle) + 重取入口 + 关键发现 + 重取提示。"""
    return (
        f"[已归档·可重取] {handle}\n"
        f"关键发现: {_clip(finding, _FINDING_MAX)}\n"
        f"(原始结果已省略;重取=重调上述工具同参查询)"
    )


def _first_key_line(content: str) -> str:
    """取结果第一行非空(裁剪)作为关键行 fallback。"""
    for line in str(content).splitlines():
        s = line.strip()
        if s:
            return _clip(s, _SNIPPET_MAX)
    return "(空结果)"


def _clip(s: Any, n: int) -> str:
    """单行化 + 裁剪到 n 字符(防换行/超长撑爆占位)。"""
    s = str(s).replace("\n", " ").replace("\r", " ").strip()
    return s if len(s) <= n else s[:n] + "..."


def _safe_json(content: str) -> dict[str, Any] | None:
    """安全解析 JSON;非 dict 或解析失败返回 None。"""
    try:
        data = json.loads(content)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None
