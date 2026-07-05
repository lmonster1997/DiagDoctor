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

    BASELINE 版本（dev-convergence-redesign 分支）：
    - 只保留硬约束：MAX_TOOL_CALLS 迭代上限 / MAX_TOKENS_BUDGET / MAX_TIME_SECONDS
    - 不做收敛检测、不做 phase 推进、不做兜底合成
    - agent 自然 stop（无 tool_calls）→ 解析最后一条 AIMessage 为 JSON
    - agent 跑满迭代 / 触发硬约束 → 同样解析最后一条 AIMessage，parse 失败则给空报告
    - 目的：观察 agent 在无 harness 干预下的纯行为，作为后续 case 驱动优化的 baseline

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

    # 工具调用去重缓存（效率优化，不改变 agent 决策）
    call_history: list[tuple[str, str]] = []

    # ── 运行时预算追踪（仅 telemetry，不驱动决策）──────────────
    ctx_budget = ContextBudget()
    ctx_budget.add_system_prompt(base_prompt)
    ctx_budget.add_evidence(evidence_text)
    ctx_budget.start_timer()

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

    budget_exhausted = False

    try:
        for iteration in range(MAX_TOOL_CALLS):
            ctx_budget.tick_iteration()

            # ── 硬约束检查（token / time）──────────────────────
            if (
                ctx_budget.total_used >= MAX_TOKENS_BUDGET
                or ctx_budget.elapsed_seconds >= MAX_TIME_SECONDS
            ):
                logger.warning(
                    "budget_hard_limit_hit",
                    iteration=iteration + 1,
                    total_used=ctx_budget.total_used,
                    elapsed_seconds=round(ctx_budget.elapsed_seconds, 1),
                    case_id=state.case_id,
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
            ctx_budget.add_agent_reasoning(str(response.content))

            # 无 tool_calls → Agent 认为诊断完成，自然 stop
            if not response.tool_calls:
                logger.info(
                    "agent_natural_stop",
                    iteration=iteration + 1,
                    case_id=state.case_id,
                )
                break

            # 处理本轮所有 tool_calls
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

                # ── 工具结果静态截断（防单条结果撑爆 context）──
                result_str = truncate_tool_result(tool_name, str(result))
                ctx_budget.add_tool_call(1)

                # ── 显式记录工具调用为 Langfuse SPAN ──────────────
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

        else:
            # for-else：循环跑满 MAX_TOOL_CALLS 次迭代未 break
            logger.warning(
                "max_iterations_reached",
                max_iterations=MAX_TOOL_CALLS,
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

    # ── 解析输出（baseline：不兜底，parse 失败就给空报告）────────
    agent_result: dict[str, Any] = {"messages": messages}
    report = parse_diagnosis_report(agent_result)
    findings = extract_findings(agent_result)

    budget_state = update_budget(state.budget, agent_result)
    early_stopped = is_budget_exceeded(budget_state) or budget_exhausted

    if report is None:
        best_summary = findings[0].summary if findings else "诊断未完成"
        report = DiagnosisReport(
            primary_category="",
            root_cause=best_summary,
            confidence=0.3,
            early_stopped=early_stopped,
            notes="Agent 未输出有效 JSON",
        )

    if early_stopped:
        report.early_stopped = True
        if not report.notes:
            report.notes = "预算超限，提前终止诊断"

    # ── Finalize Langfuse trace ─────────────────────────────────
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
