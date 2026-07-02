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

    # ── Golden signals ──
    if evidence.golden_signals:
        parts.append("【实时查询信号】")
        parts.append(_format_signals(evidence.golden_signals))
        parts.append(f"（共 {len(evidence.golden_signals)} 个信号）")

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
        "3. 调 get_file_content 确认根因\n"
        "4. 输出 JSON 诊断报告（confidence 必须基于工具结果）"
    )

    return "\n".join(parts)


def _format_signals(signals: list[Signal]) -> str:
    """Format golden signals compactly (max 30)."""
    lines: list[str] = []
    for sig in signals[:30]:
        tier_label = "前端" if sig.service_tier == "frontend" else "后端"
        sev_label = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}.get(sig.severity, "•")
        ref = f" [{sig.signal_id}]" if sig.signal_id else ""
        lines.append(
            f"  {sev_label} [{tier_label}] [{sig.source}/{sig.signal_type}] {sig.summary}{ref}"
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
            return DiagnosisReport(
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
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning(
                "failed_to_parse_diagnosis_report",
                error=str(exc),
                content_preview=last_ai_content[:500],
            )

    # Fallback: construct a best-effort report from raw text
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
    """Try to extract a JSON object from text (handles markdown code fences and raw JSON)."""
    # Try to find JSON in markdown code fences first
    json_pattern = r"```(?:json)?\s*\n?(.*?)\n?```"
    matches = re.findall(json_pattern, text, re.DOTALL)
    for match in matches:
        try:
            return json.loads(match)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            continue

    # Try to find raw JSON object (between { and })
    brace_pattern = r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}"
    brace_matches = re.findall(brace_pattern, text, re.DOTALL)
    for match in brace_matches:
        try:
            return json.loads(match)  # type: ignore[no-any-return]
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
        )
        logger.debug("langfuse_tracing_enabled", case_id=state.case_id)
    except (ValueError, ImportError) as lf_exc:
        logger.debug(
            "langfuse_tracing_disabled",
            case_id=state.case_id,
            reason=str(lf_exc),
        )

    # ── 手动 Agent 循环 ──────────────────────────────────────────
    try:
        finalizing_warned = False  # 只注入一次 FINALIZING 警告
        for iteration in range(MAX_TOOL_CALLS):
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
                            content="⚠️ 预算即将耗尽，请立即输出诊断 JSON。不要再调用任何工具。"
                        )
                    )
                    logger.warning(
                        "budget_finalizing_warning_injected",
                        iteration=iteration + 1,
                        usage_ratio=ctx_budget.usage_ratio,
                    )
                    finalizing_warned = True
                else:
                    # 已警告过一轮，LLM 仍未输出 → 强制终止
                    messages.append(
                        HumanMessage(
                            content="🛑 预算耗尽，禁止再调工具。请基于已有信息立即输出 JSON。"
                        )
                    )
                    logger.warning(
                        "budget_finalizing_force_stop",
                        iteration=iteration + 1,
                        usage_ratio=ctx_budget.usage_ratio,
                    )
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

            # 无 tool_calls → Agent 认为诊断完成
            if not response.tool_calls:
                logger.info(
                    "agent_no_tool_calls",
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
                try:
                    result = await tool_map[tool_name].ainvoke(tool_args)
                except Exception as tool_exc:
                    logger.warning(
                        "tool_execution_error",
                        tool_name=tool_name,
                        error=str(tool_exc),
                        iteration=iteration + 1,
                    )
                    result = f"工具执行错误: {tool_exc}"

                # ── 工具结果截断（方向4）────────────────────────
                result_str = truncate_tool_result(tool_name, str(result))
                # TODO(方向12): registry.run_post(tool_name, result_str)

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
                    budget_tool_tokens=ctx_budget.tool_result_tokens,
                    budget_agent_tokens=ctx_budget.agent_reasoning_tokens,
                )

        else:
            # 循环耗尽（MAX_TOOL_CALLS 次迭代用完）
            logger.warning(
                "max_tool_calls_reached",
                max_calls=MAX_TOOL_CALLS,
                case_id=state.case_id,
            )

        # Finalize Langfuse trace
        if langfuse_handler is not None:
            try:
                last_msg_content = str(messages[-1].content)[:500] if messages else ""
                langfuse_handler.end_trace(
                    output_data={"result": last_msg_content},
                )
            except Exception as lf_exc:
                logger.debug(
                    "langfuse_end_trace_error",
                    case_id=state.case_id,
                    error=str(lf_exc),
                )

    except Exception as exc:
        logger.error("unified_agent_exception", error=str(exc), case_id=state.case_id)
        if langfuse_handler is not None:
            with contextlib.suppress(Exception):
                langfuse_handler.end_trace(output_data={"error": str(exc)})
        return handle_agent_failure(state, exc)

    # ── 解析输出（复用现有函数）──────────────────────────────────
    # 将 messages 包装为 agent_result 格式，兼容现有解析函数
    agent_result: dict[str, Any] = {"messages": messages}
    report = parse_diagnosis_report(agent_result)
    findings = extract_findings(agent_result)

    # Update budget
    budget_state = update_budget(state.budget, agent_result)
    early_stopped = is_budget_exceeded(budget_state)

    # If report is None but we have findings, construct best-effort
    if report is None:
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
