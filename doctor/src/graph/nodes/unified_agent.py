"""
UnifiedAgent LangGraph node — wraps the V3 ReAct agent as a graph node.

Connects the UnifiedAgent subgraph into the main DiagDoctor graph.
Formats normalized evidence from DoctorState, invokes the ReAct agent,
and parses the result into DiagnosisReport + Findings.

Key design:
    - Evidence is passed via HumanMessage at runtime (NOT in system prompt)
    - Agent output is parsed as JSON → DiagnosisReport
    - Budget tracking is updated from agent result messages
    - On failure, falls back to best-effort report from available evidence

Usage (in main_graph.py)::

    from src.graph.nodes.unified_agent import unified_agent_node

    g.add_node("unified_agent", unified_agent_node)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from datetime import UTC, datetime
from typing import Any

import tiktoken
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from src.graph.context_engine import (
    ContextBudget,
    ContextPhase,
    build_dynamic_system_prompt,
    maybe_compact_context,
    truncate_tool_result,
)
from src.graph.state import (
    BudgetState,
    Correlation,
    DiagnosisReport,
    DoctorState,
    Finding,
    NormalizedEvidence,
    Signal,
)
from src.observability.logger import get_logger
from src.observability.tracing import traced

logger = get_logger(__name__)

# ── Token 编码器（cl100k_base，模块级缓存，避免重复构造）──────────
_encoder = tiktoken.get_encoding("cl100k_base")


def estimate_tokens(text: str) -> int:
    """精确估算 token 数（cl100k_base 编码，适用于 OpenAI 兼容模型）。"""
    return len(_encoder.encode(text))


# ── Budget constants ─────────────────────────────────────────────────

MAX_TOOL_CALLS = 12
BUDGET_WARNING_THRESHOLD = 8  # Start considering best-effort at 8 calls
MAX_TOKENS_BUDGET = 100_000  # Soft cap on total tokens
MAX_TIME_SECONDS = 300  # 5-minute timeout per diagnosis


# ═════════════════════════════════════════════════════════════════════
# Evidence formatting
# ═════════════════════════════════════════════════════════════════════


def format_evidence_for_agent(evidence: NormalizedEvidence) -> str:
    """
    将 NormalizedEvidence 格式化为 Agent 的 HumanMessage。

    Ingest 节点已完成 Loki/Tempo 实时查询 + 标准化管线处理，
    此处仅格式化 golden_signals、correlations、frontend_error_spans 供 LLM 消费。
    """
    parts: list[str] = []

    # ── User report ──
    if evidence.user_report:
        parts.append(f"【用户报告】\n{evidence.user_report}\n")

    # ── Trigger trace_ids (precise query handles) ──
    # These W3C trace_ids belong to THIS bug trigger only. Querying Tempo/Loki
    # by them yields exactly this trigger's spans/logs — no cross-case noise.
    if evidence.trigger_trace_ids:
        tids = ", ".join(evidence.trigger_trace_ids)
        parts.append(
            "【本次触发的 trace_id】\n"
            f"  {tids}\n"
            "  💡 用 search_observability(source=\"tempo\", query=\"<某个 trace_id>\") "
            "可精准拿到本次请求的完整 Trace；查日志可用 "
            "search_observability(source=\"loki\", query='{trace_id=\"<某个 trace_id>\"}')。"
        )

    # ── Golden signals ──
    has_signals = bool(evidence.golden_signals)
    has_trace_spans = (
        evidence.frontend_span_count > 0 or evidence.backend_span_count > 0
    )

    if has_signals:
        parts.append("【实时查询信号】")
        parts.append(_format_signals(evidence.golden_signals))
        parts.append(f"（共 {len(evidence.golden_signals)} 个信号）")
    else:
        # 无错误信号场景——引导 LLM 主动调查
        parts.append("【实时查询信号】")
        parts.append("  ℹ️ （无错误/异常信号——日志和 Trace 均正常）")
        parts.append(
            "  💡 建议：根据用户报告推断 Bug 类型，主动调 search_observability "
            "查看 API 响应内容，或调 code_search / get_file_content 检查代码逻辑。"
        )

    # ── Trace availability hint ──
    if not has_trace_spans:
        parts.append(
            "\n⚠️ **缺失数据提示**：当前时间窗口内无 Trace 数据。"
            "建议先调 search_observability 获取完整 Trace。"
        )

    # ── Frontend error spans (from ingest metadata) ──
    frontend_errors = evidence.metadata.get("frontend_error_spans", [])
    if frontend_errors:
        parts.append("\n### 🔴 前端崩溃 Span (client_error)")
        for span in frontend_errors[:5]:
            name = span.get("operation_name", span.get("name", "?"))
            attrs = span.get("attributes", {})
            err_msg = attrs.get("error.message", "") or attrs.get("error", "")
            dur = span.get("duration_ms", 0)
            parts.append(f"- {name} (duration={dur}ms): {err_msg[:150]}")
            if err_msg:
                parts.append("  ⚠️ 建议调 inspect_frontend_error 分析此错误")

    # ── Correlations ──
    if evidence.correlations:
        parts.append("\n【跨层关联】")
        parts.append(_format_correlations(evidence.correlations))

    # ── Instruction ──
    parts.append(
        "\n---\n"
        "⚡ 请基于以上实时查询结果进行诊断：\n"
        "1. 分析日志和 Trace 中的错误模式\n"
        "2. 调 code_search 定位相关代码\n"
        "3. 调 get_file_content 确认代码细节\n"
        "4. 必要时调 db_query 验证数据状态（如检查字段值是否为 NULL）\n"
        "5. 输出 JSON 诊断报告（confidence 必须基于工具返回的实际数据）"
    )

    return "\n".join(parts)


def _format_signals(signals: list[Signal]) -> str:
    """Format golden signals compactly (max 30).

    Signals are classified by type (error_log / error_span / slow_span /
    repeated_query) but NOT scored — the LLM agent decides which signals
    are most relevant based on the diagnostic context.
    """
    lines: list[str] = []
    for sig in signals[:30]:
        tier_label = "前端" if sig.service_tier == "frontend" else "后端"
        sev_label = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}.get(sig.severity, "•")
        ref = f" [{sig.signal_id}]" if sig.signal_id else ""
        lines.append(
            f"  {sev_label} [{tier_label}] "
            f"[{sig.source}/{sig.signal_type}] {sig.summary}{ref}"
        )
    if not lines:
        return "  （无信号）"
    return "\n".join(lines)


def _format_correlations(correlations: list[Correlation]) -> str:
    """Format cross-layer correlations compactly (max 10)."""
    lines: list[str] = []
    for corr in correlations[:10]:
        trace_str = f" trace={corr.trace_id}" if corr.trace_id else ""
        lines.append(
            f"  - [{corr.correlation_id}]{trace_str} confidence={corr.confidence:.1f}: "
            f"{corr.description}"
        )
    if not lines:
        return "  （无关联）"
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════
# Output parsing
# ═════════════════════════════════════════════════════════════════════


def parse_diagnosis_report(agent_result: dict[str, Any]) -> DiagnosisReport | None:
    """
    Parse the UnifiedAgent's final output into a DiagnosisReport.

    Extracts JSON from the last AI message. The agent is instructed to
    output structured JSON matching the DiagnosisReport schema.

    Expected JSON format::

        {
            "primary_category": "backend_error",
            "categories": ["backend_error", "performance"],
            "symptom_tier": "frontend",
            "root_cause_tier": "backend",
            "root_cause": "...",
            "affected_file": "app/services/task_service.py",
            "affected_line": 42,
            "fix_suggestion": "...",
            "evidence_chain": ["sig-xxx"],
            "confidence": 0.85
        }

    Args:
        agent_result: The full state dict returned by ``agent.ainvoke()``.

    Returns:
        DiagnosisReport if parsing succeeded, None otherwise.
    """
    messages: list[Any] = agent_result.get("messages", [])

    # Find the last AI message
    last_ai_content = ""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            last_ai_content = str(msg.content)
            break

    if not last_ai_content:
        logger.warning("no_ai_message_in_agent_result")
        return None

    # Try to extract JSON from the response
    report_data = _extract_json_from_text(last_ai_content)

    if report_data:
        try:
            parsed_report = DiagnosisReport(
                primary_category=str(report_data.get("primary_category", "")),
                categories=_ensure_str_list(report_data.get("categories", [])),
                symptom_tier=report_data.get("symptom_tier", "backend"),
                root_cause_tier=report_data.get("root_cause_tier", "backend"),
                root_cause=str(report_data.get("root_cause", "")),
                affected_file=report_data.get("affected_file"),
                affected_line=report_data.get("affected_line"),
                fix_suggestion=str(report_data.get("fix_suggestion", "")),
                evidence_chain=_ensure_str_list(report_data.get("evidence_chain", [])),
                confidence=float(report_data.get("confidence", 0.5)),
            )
            logger.info(
                "diagnosis_report_parsed",
                primary_category=parsed_report.primary_category,
                categories=parsed_report.categories,
                confidence=parsed_report.confidence,
                affected_file=parsed_report.affected_file,
            )
            return parsed_report
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning(
                "failed_to_parse_diagnosis_report",
                error=str(exc),
                content_preview=last_ai_content[:500],
                extracted_json_keys=list(report_data.keys()) if report_data else [],
            )

    # Fallback: construct a best-effort report from raw text
    logger.warning(
        "diagnosis_json_extraction_failed",
        content_len=len(last_ai_content),
        content_tail=last_ai_content[-300:] if len(last_ai_content) > 300 else last_ai_content,
    )
    return DiagnosisReport(
        primary_category="",
        root_cause=last_ai_content[:500] if last_ai_content else "（无法解析 Agent 输出）",
        confidence=0.2,
        notes="JSON 解析失败，使用原始输出作为 root_cause",
    )


def extract_findings(agent_result: dict[str, Any]) -> list[Finding]:
    """
    Extract Finding records from the agent's intermediate steps.

    Each AI message that contains a JSON block with finding-like fields
    is parsed as a Finding. This captures the agent's incremental reasoning.

    Args:
        agent_result: The full state dict from ``agent.ainvoke()``.

    Returns:
        List of Finding objects extracted from agent messages.
    """
    messages: list[Any] = agent_result.get("messages", [])
    findings: list[Finding] = []

    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue

        content = str(msg.content)
        # Skip tool call messages (they have tool_calls, not meaningful findings)
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            continue

        # Try to extract JSON from this message
        data = _extract_json_from_text(content)
        if data and ("summary" in data or "root_cause" in data):
            with contextlib.suppress(ValueError, TypeError):
                findings.append(
                    Finding(
                        agent="unified_agent",
                        summary=str(data.get("summary", data.get("root_cause", ""))),
                        evidence_refs=_ensure_str_list(
                            data.get("evidence_refs", data.get("evidence_chain", []))
                        ),
                        affected_files=_ensure_str_list(
                            data.get("affected_files", [data.get("affected_file", "")])
                        ),
                        fix_suggestion=str(data.get("fix_suggestion", "")),
                        confidence=float(data.get("confidence", 0.5)),
                    )
                )

    return findings


def _extract_json_from_text(text: str) -> dict[str, Any] | None:
    """Try to extract a JSON object from text (handles markdown code fences, raw JSON,
    mixed natural-language+JSON output, and nested structures).

    Strategy (tried in order):
    1. 剥离 DeepSeek 工具调用标记（``<｜｜DSML｜｜...>``）——LLM 即便未绑工具
       仍会吐该标记污染内容，剥离后才能拿到前面的 JSON。
    2. Markdown code fences (```json ... ``` or ``` ... ```)
    3. Brace-depth tracking — finds the FIRST complete JSON object by counting
       depth, respecting string escapes. Handles arbitrary nesting and braces
       inside string values.
    4. Fallback: greedy scan for any balanced ``{...}`` candidate.
    """
    # ── 1. 剥离 DeepSeek 工具调用标记 ───────────────────────────
    # 形如 <｜｜DSML｜｜tool_calls>...</｜｜DSML｜｜tool_calls> 或自闭合片段。
    # 用非贪婪匹配整段移除，避免标记内的伪 JSON 干扰解析。
    if "DSML" in text or "｜｜" in text:
        text = re.sub(r"<｜｜DSML｜｜[^>]*>.*?(?:</｜｜DSML｜｜[^>]*>|$)", "", text, flags=re.DOTALL)
        # 残余的自闭合/未配对标记片段也清掉
        text = re.sub(r"<｜｜DSML｜｜[^>]*>", "", text)

    # ── 2. Markdown code fences ──────────────────────────────────
    json_pattern = r"```(?:json)?\s*\n?(.*?)\n?```"
    matches = re.findall(json_pattern, text, re.DOTALL)
    for match in matches:
        try:
            return json.loads(match)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            continue

    # ── 2. Brace-depth tracking (handles arbitrary nesting + braces in strings) ──
    result = _extract_json_by_depth(text)
    if result is not None:
        return result

    # ── 3. Fallback: json.loads on the whole text (in case it's pure JSON) ──
    try:
        return json.loads(text)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        pass

    return None


def _extract_json_by_depth(text: str) -> dict[str, Any] | None:
    """Extract JSON object(s) from text using brace-depth tracking.

    Unlike regex, this correctly handles:
    - Arbitrary nesting depth
    - Braces inside JSON string values (e.g. ``{"code": "if (x) { return; }"}``)
    - Multiple JSON candidates (tries each, returns the first valid one)

    Also tries the LAST JSON object first (LLMs tend to put JSON at the end
    after natural-language reasoning).
    """
    # Collect all { } spans with their depth
    candidates: list[tuple[int, int]] = []  # (start, end) pairs
    depth = 0
    in_string = False
    escape_next = False
    start = -1

    for i, ch in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                candidates.append((start, i + 1))
                start = -1

    if not candidates:
        return None

    # Try candidates in reverse order (last JSON object first —
    # LLMs typically output reasoning before JSON, so the last
    # ``{...}`` block is most likely the intended structured output).
    for start, end in reversed(candidates):
        candidate = text[start:end]
        try:
            return json.loads(candidate)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            continue

    return None


def _ensure_str_list(value: Any) -> list[str]:
    """Ensure a value is a list of strings."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if value and isinstance(value, str):
        return [value]
    return []


# ═════════════════════════════════════════════════════════════════════
# S1: 预算兜底报告 + 收敛检测
# ═════════════════════════════════════════════════════════════════════
#
# 失败模式（S0.4 发现）：预算耗尽时 agent 最后一条 AIMessage 是 tool_call
# 请求而非 JSON 报告 → parse_diagnosis_report 解析失败 → fallback 出
# primary_category=''/affected_file=None/fix_suggestion='' 的空字段报告，
# 即使 agent 在历史消息里已给出正确 root_cause。同一 case 两次跑 0.97→0.04
# 的方差根因即此。
#
# 两个修复：
# 1. running_hypothesis：每轮从 AIMessage 提取 partial JSON 字段 + 最强
#    root_cause 叙述 + 引用过的 file 路径，累积成「已得诊断线索」。
# 2. budget 耗尽时：先做一次「无工具的强制最终 LLM 调用」带 hypothesis
#    提示让 LLM 把线索落成 JSON；仍失败则从 hypothesis + 历史合成完整
#    结构化报告（不再交空字段）。
# 3. 收敛检测：每轮后检查「信号 + 代码位置 + 机制」三要素齐备 → 注入
#    nudge 让 agent 早交付，减少 flail 到耗尽。


# 工具分类（收敛检测用）
_SIGNAL_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "search_observability",
        "query_loki_logs",
        "query_tempo_trace",
        "search_tempo_traces",
        "inspect_frontend_error",
        "parse_browser_errors",
        "extract_stack_trace",
    }
)
_CODE_LOC_TOOL_NAMES: frozenset[str] = frozenset(
    {"code_search", "get_file_content", "source_map_resolve"}
)
_VERIFY_TOOL_NAMES: frozenset[str] = frozenset({"db_query", "analyze_trace"})

# 文件路径正则（匹配 *.py / *.ts / *.tsx / *.js / *.jsx / *.go 等，含可选路径前缀）
_FILE_PATH_RE = re.compile(
    r"(?:[\w./-]+/)?[\w-]+\.(?:py|ts|tsx|js|jsx|go|java|rs|rb|php|cs|cpp|c|h)\b",
    re.IGNORECASE,
)

# 因果语言（中英）——判断 agent 是否已给出机制解释
# 涵盖：显式因果词 + 诊断结论措辞 + 错误描述措辞
_CAUSAL_RE = re.compile(
    r"(?:因为|由于|导致|根因|根本原因|触发|引起|源自|在于|错在|漏了|缺少|缺失|"
    r"是因为|问题出在|就会报|找不到|不存在|"
    r"root cause|because|caused by|due to|leads to|results? in|stem|missing|lacks?|"
    r"forgets?|omits?|fails to|never|incorrectly|wrongly|"
    r"is undefined|is null|cannot read|TypeError)",
    re.IGNORECASE,
)

# 类别关键词→类别映射（合成报告时用）
_CATEGORY_KEYWORDS: list[tuple[str, str]] = [
    ("frontend", "frontend_crash"),
    ("前端", "frontend_crash"),
    ("crash", "frontend_crash"),
    ("backend", "backend_error"),
    ("后端", "backend_error"),
    ("5xx", "backend_error"),
    ("500", "backend_error"),
    ("exception", "backend_error"),
    ("performance", "performance"),
    ("性能", "performance"),
    ("slow", "performance"),
    ("n+1", "performance"),
    ("latency", "performance"),
    ("logic", "logic"),
    ("逻辑", "logic"),
    ("sort", "logic"),
    ("order", "logic"),
    ("data", "data"),
    ("数据", "data"),
    ("null", "data"),
    ("missing field", "data"),
    ("config", "config"),
    ("配置", "config"),
    ("env", "config"),
]


def _infer_categories_from_text(text: str) -> list[str]:
    """从文本关键词推断 bug 类别（合成报告时用，弱启发式）。"""
    lowered = text.lower()
    hits: list[str] = []
    seen: set[str] = set()
    for kw, cat in _CATEGORY_KEYWORDS:
        if kw in lowered and cat not in seen:
            hits.append(cat)
            seen.add(cat)
    return hits


def _extract_file_paths(text: str, limit: int = 3) -> list[str]:
    """从文本中提取文件路径（去重，最多 limit 个）。"""
    found: list[str] = []
    seen: set[str] = set()
    for m in _FILE_PATH_RE.finditer(text):
        path = m.group(0)
        # 过滤显然不是源码路径的噪音（如 version.py 这种常见库名可保留）
        if path not in seen:
            seen.add(path)
            found.append(path)
            if len(found) >= limit:
                break
    return found


def _update_running_hypothesis(
    hypothesis: dict[str, Any],
    ai_content: str,
    tool_calls: list[dict[str, Any]] | None,
) -> None:
    """每轮 AIMessage 后更新 running hypothesis（原地修改）。

    hypothesis 字段：
        root_cause_text: 最强 root_cause 叙述（最长且含因果词的 AIMessage 内容片段）
        primary_category / categories / affected_file / affected_line / fix_suggestion /
        evidence_chain / confidence: 从 partial JSON 提取（last non-empty wins）
        referenced_files: agent 在文本里引用过的文件路径（累积）
        probed_files: agent 通过 code_search/get_file_content 实际查看的文件（累积）
    """
    if not ai_content:
        return

    # 1. 提取 partial JSON 字段（last non-empty wins）
    data = _extract_json_from_text(ai_content)
    if data:
        for field in (
            "primary_category",
            "affected_file",
            "fix_suggestion",
            "root_cause",
        ):
            val = data.get(field)
            if val:
                hypothesis[field] = str(val)
        for field in ("categories", "evidence_chain"):
            val = data.get(field)
            if isinstance(val, list) and val:
                hypothesis[field] = [str(v) for v in val]
        line_val = data.get("affected_line")
        if isinstance(line_val, (int, float)) and line_val:
            hypothesis["affected_line"] = int(line_val)
        conf_val = data.get("confidence")
        if isinstance(conf_val, (int, float)):
            hypothesis["confidence"] = float(conf_val)

    # 2. 更新最强 root_cause 叙述：选含因果词且较长的内容
    if _CAUSAL_RE.search(ai_content):
        prev_text = hypothesis.get("root_cause_text", "") or ""
        if len(ai_content) > len(prev_text):
            # 截一段（避免整段 reasoning 太长）；取含因果词附近的窗口
            m = _CAUSAL_RE.search(ai_content)
            start = max(0, (m.start() if m else 0) - 200)
            end = min(len(ai_content), start + 800)
            hypothesis["root_cause_text"] = ai_content[start:end].strip()

    # 3. 累积引用过的文件路径
    refs = _extract_file_paths(ai_content, limit=5)
    if refs:
        hypothesis.setdefault("referenced_files", [])
        for r in refs:
            if r not in hypothesis["referenced_files"]:
                hypothesis["referenced_files"].append(r)

    # 4. 记录 agent 实际探测过的文件（从 tool_calls）
    if tool_calls:
        hypothesis.setdefault("probed_files", [])
        for tc in tool_calls:
            tname = tc.get("name", "")
            if tname in _CODE_LOC_TOOL_NAMES:
                args = tc.get("args", {}) or {}
                # code_search: query / file_path; get_file_content: file_path
                fp = args.get("file_path") or args.get("path") or args.get("file")
                if isinstance(fp, str) and fp and fp not in hypothesis["probed_files"]:
                    hypothesis["probed_files"].append(fp)


def _format_hypothesis_hint(hypothesis: dict[str, Any]) -> str:
    """把 running hypothesis 格式化成给 LLM 的「已得线索」提示。"""
    parts: list[str] = ["你已经完成了调查，以下是你在调查过程中得出的诊断线索："]

    rc = hypothesis.get("root_cause_text") or hypothesis.get("root_cause") or ""
    if rc:
        parts.append(f"\n【根因叙述】\n{rc}")

    pc = hypothesis.get("primary_category", "")
    cats = hypothesis.get("categories", [])
    if pc or cats:
        parts.append(f"\n【类别】primary={pc or '(未定)'}  categories={cats or []}")

    af = hypothesis.get("affected_file")
    if af:
        parts.append(f"\n【受影响文件】{af}")

    fs = hypothesis.get("fix_suggestion", "")
    if fs:
        parts.append(f"\n【修复建议（草稿）】{fs}")

    refs = hypothesis.get("referenced_files", []) or []
    probed = hypothesis.get("probed_files", []) or []
    if probed:
        parts.append(f"\n【你实际查看过的文件】{probed}")
    if refs:
        parts.append(f"\n【你提及过的文件】{refs}")

    ec = hypothesis.get("evidence_chain", [])
    if ec:
        parts.append(f"\n【证据链】{ec}")

    conf = hypothesis.get("confidence")
    if isinstance(conf, (int, float)):
        parts.append(f"\n【你此前的置信度】{conf}")

    parts.append(
        "\n---\n"
        "请基于以上线索，现在直接输出**完整 JSON 诊断报告**（不要再调用任何工具，"
        "不要输出任何工具调用语法或 <｜｜DSML｜｜> 标记，不要输出 JSON 以外的任何文字）。"
        "JSON 必须包含字段：primary_category, categories, symptom_tier, root_cause_tier, "
        "root_cause, affected_file, affected_line, fix_suggestion, evidence_chain, confidence。"
        "若证据不充分，confidence 取低值（0.3-0.5）并在 root_cause 中说明缺口。"
        "如果线索里某个字段为空，请基于你的最佳判断补全（不要留空）。"
        "输出格式：```json\\n{...}\\n```"
    )
    return "\n".join(parts)


def _synthesize_fallback_report(
    hypothesis: dict[str, Any],
    messages: list[BaseMessage],
    early_stopped: bool,
) -> DiagnosisReport:
    """从 running hypothesis + 消息历史合成完整结构化报告（兜底，不交空字段）。

    仅在「强制最终 LLM 调用仍失败」时启用——尽力保证报告字段非空。
    """
    # root_cause：优先 hypothesis.root_cause_text，否则扫所有 AIMessage 找含因果词的最长段；
    # 若都没有因果词，退而取最长的 substantive AIMessage（>80 字符）——避免空 root_cause
    # 导致 judge 打 0 分。LLM 的因果叙述措辞多样（"就会报这个错"/"是因为"/"问题出在"），
    # _CAUSAL_RE 不一定全覆盖，所以有这个兜底。
    rc_text = hypothesis.get("root_cause_text") or hypothesis.get("root_cause") or ""
    if not rc_text:
        best_snippet = ""
        best_fallback = ""
        for msg in messages:
            if not isinstance(msg, AIMessage):
                continue
            content = str(msg.content)
            if not content or len(content) < 30:
                continue
            # 跟踪最长的 substantive 段（兜底用）
            if len(content) > len(best_fallback):
                best_fallback = content
            # 优先找含因果词的段
            if _CAUSAL_RE.search(content) and len(content) > len(best_snippet):
                m = _CAUSAL_RE.search(content)
                start = max(0, (m.start() if m else 0) - 200)
                end = min(len(content), start + 800)
                best_snippet = content[start:end].strip()
        rc_text = best_snippet or (best_fallback[:800] if best_fallback else "")
        if rc_text and not best_snippet:
            # 来自兜底段，截取含文件/函数引用附近的内容
            rc_text = rc_text.strip()
    if not rc_text:
        rc_text = "（预算耗尽，未能确定根因）"

    # affected_file：优先 root_cause_text 里出现的文件（最可能是真因文件），
    # 否则 hypothesis.affected_file，否则 probed_files 里在 root_cause_text 出现过的，
    # 否则 probed_files[-1]，否则 referenced_files[-1]
    affected_file = hypothesis.get("affected_file")
    if not affected_file:
        rc_lower = rc_text.lower()
        # 1) 先从 root_cause_text 提取文件路径（这些是 agent 在解释根因时引用的文件）
        rc_files = _extract_file_paths(rc_text, limit=5)
        if rc_files:
            affected_file = rc_files[0]
        else:
            # 2) probed_files 里在 root_cause_text 出现过的
            probed = hypothesis.get("probed_files", []) or []
            matched = [p for p in probed if p.lower() in rc_lower]
            if matched:
                affected_file = matched[-1]
            elif probed:
                affected_file = probed[-1]
            else:
                refs = hypothesis.get("referenced_files", []) or []
                if refs:
                    affected_file = refs[-1]

    # categories：优先 hypothesis，否则从 root_cause 文本推断
    categories = hypothesis.get("categories", []) or []
    primary_category = hypothesis.get("primary_category", "") or (categories[0] if categories else "")
    if not categories:
        inferred = _infer_categories_from_text(rc_text)
        categories = inferred
        if not primary_category and inferred:
            primary_category = inferred[0]

    # fix_suggestion：优先 hypothesis，否则从 root_cause 派生一句
    fix_suggestion = hypothesis.get("fix_suggestion", "") or ""
    if not fix_suggestion and affected_file:
        fix_suggestion = (
            f"建议在 {affected_file} 中检查与根因相关的逻辑：{rc_text[:200]}。"
            "（预算耗尽，未及给出精确修复方案）"
        )

    evidence_chain = hypothesis.get("evidence_chain", []) or []
    confidence = hypothesis.get("confidence")
    if not isinstance(confidence, (int, float)):
        # 兜底合成：置信度保守
        confidence = 0.35 if affected_file else 0.2

    affected_line = hypothesis.get("affected_line")

    report = DiagnosisReport(
        primary_category=primary_category or "",
        categories=categories,
        symptom_tier="backend",
        root_cause_tier="backend",
        root_cause=rc_text[:1500] if rc_text else "",
        affected_file=affected_file,
        affected_line=affected_line,
        fix_suggestion=fix_suggestion,
        evidence_chain=evidence_chain,
        confidence=float(confidence),
        early_stopped=True,
        notes=(
            "预算耗尽兜底合成报告（S1）：agent 在历史消息中已得诊断线索，"
            "强制最终 LLM 调用未能产出完整 JSON，由 harness 从 running hypothesis 合成。"
            if early_stopped
            else "harness 兜底合成报告（agent 未交付完整 JSON）。"
        ),
    )
    logger.warning(
        "s1_fallback_report_synthesized",
        primary_category=report.primary_category,
        affected_file=report.affected_file,
        confidence=report.confidence,
        had_root_cause_text=bool(rc_text),
        early_stopped=early_stopped,
    )
    return report


def _report_is_incomplete(report: DiagnosisReport | None) -> bool:
    """判断报告是否「字段不完整」——需要兜底。"""
    if report is None:
        return True
    # 三个关键结构化字段全空 → 视为不完整（即使 root_cause 有内容也不算交付）
    return (
        not report.primary_category
        and not report.affected_file
        and not report.fix_suggestion
    )


def _detect_convergence(
    has_signal: bool,
    has_code_loc: bool,
    latest_ai_content: str,
    iteration: int,
    min_iterations: int = 3,
) -> bool:
    """收敛检测：信号 + 代码位置 + 机制三要素齐备且已达最小轮数。

    机制判定：最新 AIMessage 含因果词且引用了文件/函数/行号（说明 agent 已形成解释）。
    """
    if iteration + 1 < min_iterations:
        return False
    if not (has_signal and has_code_loc):
        return False
    if not latest_ai_content or not _CAUSAL_RE.search(latest_ai_content):
        return False
    # 必须引用了具体位置（文件路径或行号或函数）
    has_loc_ref = bool(
        _FILE_PATH_RE.search(latest_ai_content)
        or re.search(r"(?:line|行|L)\s*\d+", latest_ai_content, re.IGNORECASE)
        or re.search(r"\b\w+\(\)", latest_ai_content)
    )
    return has_loc_ref


def _detect_flailing(
    iteration_tool_categories: list[set[str]],
    probed_files_counts: dict[str, int],
    iteration: int,
    consecutive_code_loc_only: int = 6,
    min_iterations: int = 6,
    reprobe_threshold: int = 3,
) -> bool:
    """flailing 检测：agent 在兜圈子，没在推进诊断。

    判据（任一满足且 iteration >= min_iterations）：
    1. 连续 ``consecutive_code_loc_only`` 轮**只**用 code-loc 工具（无 signal/verify）——
       agent 在反复读代码却没取新信号或验证假设。阈值 6 区分合法深挖（FE-020 确认
       tags 缺失需 5 轮 code-loc / LOGIC-020 连 4 轮后接 verify）与真 flailing
       （BE-020 unlucky 连 10 轮）。
    2. 某个文件被探测（code_search/get_file_content）>= ``reprobe_threshold`` 次——
       agent 在反复读同一个文件。

    与收敛检测互补：收敛检测在「证据齐备」时让 agent 早交付；
    flailing 检测在「agent 卡住」时强制它交付 best-guess。
    """
    if iteration + 1 < min_iterations:
        return False

    # 判据 1：连续 N 轮只用 code-loc 工具
    if len(iteration_tool_categories) >= consecutive_code_loc_only:
        recent = iteration_tool_categories[-consecutive_code_loc_only:]
        if all(cats == {"code_loc"} for cats in recent):
            return True

    # 判据 2：某个文件被反复探测
    if any(count >= reprobe_threshold for count in probed_files_counts.values()):
        return True

    return False


# ═════════════════════════════════════════════════════════════════════
# Budget tracking
# ═════════════════════════════════════════════════════════════════════


def update_budget(budget: BudgetState, agent_result: dict[str, Any]) -> BudgetState:
    """
    Update budget state from agent execution result.

    Counts tool calls and estimates token usage from messages.

    Args:
        budget: Current budget state.
        agent_result: Result dict from agent invocation.

    Returns:
        Updated BudgetState.
    """
    messages: list[Any] = agent_result.get("messages", [])
    tool_call_count = 0

    for msg in messages:
        if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls") and msg.tool_calls:
            tool_call_count += len(msg.tool_calls)

    # Estimate tokens using tiktoken (cl100k_base, handles Chinese/English/code accurately)
    total_tokens = sum(estimate_tokens(str(m.content)) for m in messages if hasattr(m, "content"))

    now = datetime.now(UTC)
    elapsed = (now - budget.started_at).total_seconds() if budget.started_at else 0.0

    return BudgetState(
        total_tokens=budget.total_tokens + total_tokens,
        total_cost_usd=budget.total_cost_usd,  # Updated externally by cost accountant
        tool_calls=budget.tool_calls + tool_call_count,
        started_at=budget.started_at or now,
        elapsed_seconds=elapsed,
        last_checked_at=now,
    )


def is_budget_exceeded(budget: BudgetState) -> bool:
    """
    Check if the diagnosis budget has been exceeded.

    Returns True if any of:
    - Tool calls >= MAX_TOOL_CALLS (12)
    - Estimated tokens >= MAX_TOKENS_BUDGET (100k)
    - Elapsed time >= MAX_TIME_SECONDS (300s)
    """
    if budget.tool_calls >= MAX_TOOL_CALLS:
        return True
    return budget.total_tokens >= MAX_TOKENS_BUDGET or budget.elapsed_seconds >= MAX_TIME_SECONDS


# ═════════════════════════════════════════════════════════════════════
# Failure handling
# ═════════════════════════════════════════════════════════════════════


def handle_agent_failure(state: DoctorState, error: Exception) -> dict[str, Any]:
    """
    Handle agent failures gracefully — produce a best-effort fallback report.

    Args:
        state: Current DoctorState before the failure.
        error: The exception that caused the failure.

    Returns:
        Dict with fallback report and findings for state merge.
    """
    logger.error("unified_agent_failure", error=str(error), case_id=state.case_id)

    return {
        "report": DiagnosisReport(
            primary_category="",
            categories=[],
            root_cause=f"诊断 Agent 执行失败：{error}",
            confidence=0.0,
            early_stopped=True,
            notes=f"Agent 异常终止: {error}",
        ),
        "findings": [
            Finding(
                agent="unified_agent",
                summary=f"Agent 执行失败：{error}",
                confidence=0.0,
            )
        ],
        "early_stopped": True,
    }


# ═════════════════════════════════════════════════════════════════════
# Node function
# ═════════════════════════════════════════════════════════════════════


@traced()
async def unified_agent_node(state: DoctorState) -> dict[str, Any]:
    """
    LangGraph node: unified diagnosis — ingest 后的唯一步骤.

    Ingest 节点已完成 Loki/Tempo 实时查询 + 标准化管线处理，
    此处仅负责格式化证据 → LLM 诊断，不执行任何数据获取。

    核心结构：
    1. 格式化证据：NormalizedEvidence → HumanMessage
    2. 手动循环：逐轮调用 LLM → 执行工具 → 工具结果入 messages
    3. 工具调用去重：相同参数的工具调用自动跳过
    4. 解析输出：复用现有的 parse_diagnosis_report / extract_findings

    Args:
        state: Current DoctorState (after Ingest).

    Returns:
        Dict with report, findings, budget, early_stopped for state merge.
    """
    from src.graph.subgraphs.unified_agent import _build_system_prompt
    from src.llm_factory import get_llm_for_role
    from src.tools import get_all_tools

    evidence: NormalizedEvidence = state.evidence

    # Expose trigger_time to search_observability via ContextVar so the tool
    # defaults its query window to trigger_time ± 5min (per-case isolation)
    # instead of "last 1 hour" (which in batch runs contains logs from other
    # cases and pollutes the diagnosis).
    try:
        from src.tools.observability_unified import set_trigger_time

        set_trigger_time(evidence.trigger_time)
    except (ImportError, AttributeError):
        pass

    # Format evidence for the agent
    evidence_text = format_evidence_for_agent(evidence)

    logger.info(
        "unified_agent_invoking",
        case_id=state.case_id,
        signal_count=len(evidence.golden_signals),
        correlation_count=len(evidence.correlations),
    )

    # ── 构建消息列表 ─────────────────────────────────────────────
    base_prompt = _build_system_prompt()
    messages: list[BaseMessage] = [
        SystemMessage(content=base_prompt),
        HumanMessage(content=evidence_text),
    ]

    # ── 准备 LLM + 工具 ──────────────────────────────────────────
    llm = get_llm_for_role("diagnosis")
    tools = get_all_tools()
    tool_map = {t.name: t for t in tools}
    llm_with_tools = llm.bind_tools(tools)

    # 工具调用去重缓存
    call_history: list[tuple[str, str]] = []

    # ── 运行时预算追踪 ──────────────────────────────────────────
    # ContextBudget 提供 token 精准追踪 + 阶段判定 + 阈值告警
    ctx_budget = ContextBudget()
    ctx_budget.add_system_prompt(base_prompt)
    ctx_budget.add_evidence(evidence_text)

    # ── Langfuse LLM tracing (graceful degradation) ──────────────
    invoke_config: dict[str, Any] = {}
    langfuse_handler = None
    try:
        from src.observability.langfuse_tracing import get_langfuse_handler

        langfuse_handler = get_langfuse_handler()
        invoke_config["callbacks"] = [langfuse_handler]
        langfuse_handler.start_trace(
            input_data={"evidence": evidence_text[:500]},
            trace_id=state.langfuse_trace_id,
        )
        logger.debug(
            "langfuse_tracing_enabled",
            case_id=state.case_id,
            reused_trace_id=state.langfuse_trace_id is not None,
        )
    except (ValueError, ImportError) as lf_exc:
        logger.debug(
            "langfuse_tracing_disabled",
            case_id=state.case_id,
            reason=str(lf_exc),
        )

    # ── 手动 Agent 循环 ──────────────────────────────────────────
    # S1: running_hypothesis 累积 agent 已得诊断线索（用于预算耗尽兜底）
    running_hypothesis: dict[str, Any] = {}
    # S1: 收敛检测状态
    convergence_nudged = False
    has_signal_evidence = bool(evidence.golden_signals) or bool(
        evidence.metadata.get("frontend_error_spans")
    )
    has_code_loc = False
    # S1: budget 耗尽标记（区分自然交付 vs 强制终止）
    budget_exhausted = False

    # S1.5: flailing 检测状态
    iteration_tool_categories: list[set[str]] = []  # 每轮用过的工具类别集合
    probed_files_counts: dict[str, int] = {}  # 文件路径 -> 被探测次数
    flailing_warned = False  # 是否已注入过 flailing nudge

    # S1.5: 启动总诊断计时器 + 配置 iteration/tool_calls 上限
    ctx_budget.start_timer()
    ctx_budget.max_iterations = MAX_TOOL_CALLS
    ctx_budget.max_tool_calls = 18  # 辅助计量：12 迭代里塞 18+ 工具调用 → FINALIZING
    ctx_budget.max_time_seconds = 180.0  # 总诊断 3 分钟帽

    try:
        finalizing_warned = False  # 只注入一次 FINALIZING 警告
        for iteration in range(MAX_TOOL_CALLS):
            # S1.5: 推进迭代计数（phase 据此判定）
            ctx_budget.tick_iteration()

            # ── 上下文压缩（方向4）───────────────────────────────
            messages, compacted = maybe_compact_context(messages, ctx_budget)

            # ── 动态 System Prompt（方向6）───────────────────────
            dynamic_prompt = build_dynamic_system_prompt(base_prompt, ctx_budget)
            messages[0] = SystemMessage(content=dynamic_prompt)

            # ── FINALIZING 阶段：注入警告（只一次），不 break ─────
            # 首次 FINALIZING → 注入警告让 LLM 自然输出 JSON
            # 若 LLM 忽略警告仍调工具，下轮再次 FINALIZING 时强制终止
            if ctx_budget.phase == ContextPhase.FINALIZING:
                if not finalizing_warned:
                    messages.append(
                        HumanMessage(
                            content="⏳ 预算已接近上限。请基于已有证据，以 JSON 格式给出你的最终诊断结论。"
                            "如果证据不充分，请在 confidence 和 notes 中反映这一点。"
                        )
                    )
                    logger.warning(
                        "budget_finalizing_warning_injected",
                        iteration=iteration + 1,
                        usage_ratio=ctx_budget.usage_ratio,
                        tool_calls=ctx_budget.tool_calls,
                        elapsed_seconds=round(ctx_budget.elapsed_seconds, 1),
                    )
                    finalizing_warned = True
                else:
                    # 已警告过一轮，LLM 仍未输出 → 强制终止
                    # S1: 不再 break 后丢空字段；标记 budget_exhausted，
                    # 循环外做「强制最终 LLM 调用 + 兜底合成」
                    messages.append(
                        HumanMessage(
                            content="🛑 本轮必须给出结论。请基于已有信息，用 JSON 格式输出诊断报告。"
                            "降低 confidence 来反映证据的不完整程度。"
                        )
                    )
                    logger.warning(
                        "budget_finalizing_force_stop",
                        iteration=iteration + 1,
                        usage_ratio=ctx_budget.usage_ratio,
                        tool_calls=ctx_budget.tool_calls,
                        elapsed_seconds=round(ctx_budget.elapsed_seconds, 1),
                    )
                    budget_exhausted = True
                    break

            response: AIMessage = await asyncio.wait_for(
                llm_with_tools.ainvoke(
                    messages,
                    config=invoke_config if invoke_config else None,  # type: ignore[arg-type]
                ),
                timeout=MAX_TIME_SECONDS,
            )
            messages.append(response)

            # 更新 Agent 推理 token 预算
            ctx_budget.add_agent_reasoning(str(response.content))

            # S1: 更新 running hypothesis（每轮 AIMessage 后）
            _update_running_hypothesis(
                running_hypothesis,
                str(response.content),
                getattr(response, "tool_calls", None),
            )

            # 无 tool_calls → Agent 认为诊断完成
            if not response.tool_calls:
                logger.info(
                    "agent_no_tool_calls",
                    iteration=iteration + 1,
                    case_id=state.case_id,
                )
                break

            # 处理本轮所有 tool_calls
            iter_cats: set[str] = set()  # S1.5: 本轮用过的工具类别
            for tc in response.tool_calls:
                tool_name = tc["name"]
                tool_args = tc["args"]

                # ── 工具调用去重 ─────────────────────────────────
                call_key = (tool_name, json.dumps(tool_args, sort_keys=True))
                if call_key in call_history:
                    logger.debug(
                        "tool_call_skipped_duplicate",
                        tool_name=tool_name,
                        iteration=iteration + 1,
                    )
                    # 记录去重跳过事件（轻量 EVENT，不 inflate 真实工具调用数）
                    if langfuse_handler is not None:
                        with contextlib.suppress(Exception):
                            langfuse_handler.record_tool_skipped(
                                tool_name=tool_name,
                                tool_args=tool_args,
                                iteration=iteration + 1,
                            )
                    messages.append(
                        ToolMessage(
                            content="[跳过：与之前调用完全相同]",
                            tool_call_id=tc["id"],
                            name=tool_name,
                        )
                    )
                    continue
                call_history.append(call_key)

                # TODO(方向12): registry.run_pre(tool_name, tool_args)
                # TODO(方向10): recorder.record_tool_call(...)

                # ── 执行工具（错误不中断循环）────────────────────
                tool_t0 = time.monotonic()
                tool_error: str | None = None
                try:
                    result = await tool_map[tool_name].ainvoke(tool_args)
                except Exception as tool_exc:
                    logger.warning(
                        "tool_execution_error",
                        tool_name=tool_name,
                        error=str(tool_exc),
                        iteration=iteration + 1,
                    )
                    tool_error = str(tool_exc)
                    result = f"工具执行错误: {tool_exc}"
                tool_latency_ms = (time.monotonic() - tool_t0) * 1000

                # ── 工具结果截断（方向4）────────────────────────
                result_str = truncate_tool_result(tool_name, str(result))
                # TODO(方向12): registry.run_post(tool_name, result_str)

                # S1: 收敛检测——更新证据三要素标志
                if tool_name in _SIGNAL_TOOL_NAMES and result_str and "工具执行错误" not in result_str:
                    has_signal_evidence = True
                    iter_cats.add("signal")
                if tool_name in _CODE_LOC_TOOL_NAMES and result_str and "工具执行错误" not in result_str:
                    has_code_loc = True
                    iter_cats.add("code_loc")
                    # S1.5: 记录被探测的文件路径（用于 flailing reprobe 检测）
                    fp = tool_args.get("file_path") or tool_args.get("path") or tool_args.get("file")
                    if isinstance(fp, str) and fp:
                        probed_files_counts[fp] = probed_files_counts.get(fp, 0) + 1
                if tool_name in _VERIFY_TOOL_NAMES and result_str and "工具执行错误" not in result_str:
                    iter_cats.add("verify")
                # S1.5: 计入工具调用预算（辅助计量，防 12 迭代塞 30 调用）
                ctx_budget.add_tool_call(1)

                # ── 显式记录工具调用为 Langfuse SPAN ──────────────
                # 手动循环中 tool.ainvoke 未传 callback config，
                # on_tool_start/on_tool_end 不会触发，因此显式记录。
                if langfuse_handler is not None:
                    with contextlib.suppress(Exception):
                        langfuse_handler.record_tool_span(
                            tool_name=tool_name,
                            tool_args=tool_args,
                            result=result_str,
                            latency_ms=tool_latency_ms,
                            iteration=iteration + 1,
                            error=tool_error,
                        )

                # 更新工具结果 token 预算
                ctx_budget.add_tool_result(result_str)

                messages.append(
                    ToolMessage(
                        content=result_str,
                        tool_call_id=tc["id"],
                        name=tool_name,
                    )
                )

                logger.debug(
                    "tool_executed",
                    tool_name=tool_name,
                    iteration=iteration + 1,
                    result_len=len(result_str),
                    latency_ms=round(tool_latency_ms, 1),
                    budget_tool_tokens=ctx_budget.tool_result_tokens,
                    budget_agent_tokens=ctx_budget.agent_reasoning_tokens,
                )

            # S1: 收敛检测——本轮工具处理完后，若三要素齐备则注入 nudge（仅一次）
            iteration_tool_categories.append(iter_cats)
            if (
                not convergence_nudged
                and _detect_convergence(
                    has_signal_evidence,
                    has_code_loc,
                    str(response.content),
                    iteration,
                )
            ):
                messages.append(
                    HumanMessage(
                        content="✅ 证据已充分（错误信号 + 代码位置 + 机制解释均已获得）。"
                        "请直接以 JSON 格式输出最终诊断报告，不要再调用工具。"
                        "confidence 应反映证据强度。"
                    )
                )
                convergence_nudged = True
                logger.info(
                    "s1_convergence_nudge_injected",
                    iteration=iteration + 1,
                    case_id=state.case_id,
                )

            # S1.5: flailing 检测——agent 在兜圈子时强制它交付 best-guess
            # 与收敛检测互补：收敛在「证据齐备」时早交付；flailing 在「卡住」时强制交付。
            if not flailing_warned and _detect_flailing(
                iteration_tool_categories,
                probed_files_counts,
                iteration,
            ):
                messages.append(
                    HumanMessage(
                        content="⚠️ 你似乎在反复检索代码却没有推进诊断（连续多轮只读代码未取新信号，"
                        "或反复读同一个文件）。如果证据已足够支撑诊断，请以 JSON 格式输出 best-guess "
                        "诊断报告；证据不充分时 confidence 取低值（0.3-0.5）并在 notes 说明缺口。"
                    )
                )
                flailing_warned = True
                logger.warning(
                    "s1_5_flailing_nudge_injected",
                    iteration=iteration + 1,
                    case_id=state.case_id,
                    iter_cats=iteration_tool_categories[-3:],
                    probed_files_counts={
                        k: v for k, v in probed_files_counts.items() if v >= 2
                    },
                )
            # 注：flailing 只软 nudge，不 force-stop——force-stop 会误杀合法的多文件
            # 深挖（如 FE-020 确认 tags 字段缺失需读 5+ 个文件）。最终交付由 iteration-based
            # FINALIZING（iter 10+）+ S1 兜底保证，flailing nudge 只是提前提醒。

        else:
            # 循环耗尽（MAX_TOOL_CALLS 次迭代用完）
            logger.warning(
                "max_tool_calls_reached",
                max_calls=MAX_TOOL_CALLS,
                case_id=state.case_id,
                tool_calls=ctx_budget.tool_calls,
                elapsed_seconds=round(ctx_budget.elapsed_seconds, 1),
            )
            budget_exhausted = True

    except Exception as exc:
        logger.error("unified_agent_exception", error=str(exc), case_id=state.case_id)
        if langfuse_handler is not None:
            with contextlib.suppress(Exception):
                langfuse_handler.end_trace(output_data={"error": str(exc)})
        return handle_agent_failure(state, exc)

    # ── S1: 预算耗尽兜底——强制最终 LLM 调用 + 合成 ──────────────
    # 当 budget 耗尽时，agent 最后一条 AIMessage 可能是 tool_call 请求而非
    # JSON 报告。parse_diagnosis_report 会失败并 fallback 出空字段报告。
    # 修复：先用 running hypothesis 做一次「无工具的强制最终 LLM 调用」，
    # 让 LLM 把已得线索落成 JSON；仍失败则由 harness 合成完整报告。
    forced_final_done = False
    if budget_exhausted:
        agent_result_check: dict[str, Any] = {"messages": messages}
        pre_report = parse_diagnosis_report(agent_result_check)
        if _report_is_incomplete(pre_report):
            logger.info(
                "s1_forced_final_llm_call",
                case_id=state.case_id,
                hypothesis_keys=list(running_hypothesis.keys()),
                convergence_nudged=convergence_nudged,
            )
            try:
                hint = _format_hypothesis_hint(running_hypothesis)
                forced_messages = messages + [SystemMessage(content=hint)]
                # 无工具绑定——LLM 只能输出文本/JSON，不能再 flail
                forced_response: AIMessage = await asyncio.wait_for(
                    llm.ainvoke(
                        forced_messages,
                        config=invoke_config if invoke_config else None,  # type: ignore[arg-type]
                    ),
                    timeout=MAX_TIME_SECONDS,
                )
                messages.append(forced_response)
                ctx_budget.add_agent_reasoning(str(forced_response.content))
                forced_final_done = True
                logger.info(
                    "s1_forced_final_response_received",
                    case_id=state.case_id,
                    response_len=len(str(forced_response.content)),
                )
            except Exception as ff_exc:
                logger.warning(
                    "s1_forced_final_call_failed",
                    case_id=state.case_id,
                    error=str(ff_exc),
                )

    # ── 解析输出（复用现有函数）──────────────────────────────────
    # 将 messages 包装为 agent_result 格式，兼容现有解析函数
    agent_result: dict[str, Any] = {"messages": messages}
    report = parse_diagnosis_report(agent_result)
    findings = extract_findings(agent_result)

    # Update budget
    budget_state = update_budget(state.budget, agent_result)
    early_stopped = is_budget_exceeded(budget_state) or budget_exhausted

    # S1: 若报告字段仍不完整（含 forced_final 仍失败的情况）→ 合成兜底报告
    if _report_is_incomplete(report):
        logger.warning(
            "s1_report_incomplete_synthesizing",
            case_id=state.case_id,
            budget_exhausted=budget_exhausted,
            forced_final_done=forced_final_done,
        )
        report = _synthesize_fallback_report(
            running_hypothesis,
            messages,
            early_stopped=early_stopped,
        )
    elif report is None:
        best_summary = findings[0].summary if findings else "诊断未完成"
        report = DiagnosisReport(
            primary_category="",
            root_cause=best_summary,
            confidence=0.3,
            early_stopped=early_stopped,
            notes="Agent 未输出有效 JSON，使用 best-effort 报告",
        )

    # Set early_stopped on the report
    if early_stopped:
        report.early_stopped = True
        if not report.notes:
            report.notes = "预算超限，提前终止诊断"

    # ── Finalize Langfuse trace（报告解析后，输出结构化诊断）─────
    # 注意：必须在 parse_diagnosis_report 之后调用，否则 trace 的
    # 顶层 output 会是 messages[-1]（可能是 ToolMessage=原始工具结果），
    # 而非最终诊断报告。
    if langfuse_handler is not None:
        try:
            report_dict = (
                report.model_dump(mode="json") if hasattr(report, "model_dump") else {}
            )
            langfuse_handler.end_trace(
                output_data={
                    "diagnosis_report": report_dict,
                    "early_stopped": early_stopped,
                    "tool_calls": budget_state.tool_calls,
                },
            )
            logger.debug(
                "langfuse_trace_finalized",
                case_id=state.case_id,
                primary_category=report.primary_category,
                confidence=report.confidence,
                early_stopped=early_stopped,
            )
        except Exception as lf_exc:
            logger.debug(
                "langfuse_end_trace_error",
                case_id=state.case_id,
                error=str(lf_exc),
            )

    logger.info(
        "unified_agent_completed",
        case_id=state.case_id,
        primary_category=report.primary_category,
        confidence=report.confidence,
        tool_calls=budget_state.tool_calls,
        early_stopped=early_stopped,
    )

    return {
        "report": report,
        "findings": findings,
        "budget": budget_state,
        "early_stopped": early_stopped,
    }
