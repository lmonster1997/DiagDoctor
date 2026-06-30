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

import contextlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

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

    新架构（实时查询）：不再接收预收集日志/Trace，
    unified_agent_node 已通过 auto-prefetch 从 Loki/Tempo 实时获取数据并注入为 golden_signals。
    """
    parts: list[str] = []

    # ── User report ──
    if evidence.user_report:
        parts.append(f"【用户报告】\n{evidence.user_report}\n")

    # ── Golden signals + auto-prefetch 数据 ──
    if evidence.golden_signals:
        parts.append("【实时查询信号】")
        parts.append(_format_signals(evidence.golden_signals))

        # 展示 auto-prefetch 的原始数据
        for sig in evidence.golden_signals:
            if sig.signal_id == "sig-auto-prefetch" and sig.metadata.get("prefetch_data"):
                prefetch = sig.metadata["prefetch_data"]
                logs = prefetch.get("logs", [])
                traces = prefetch.get("traces", [])
                parts.append(f"\n【预查询原始数据】Loki={len(logs)}条, Tempo={len(traces)}条")

                if logs:
                    parts.append("\n### 最近日志（前3条）")
                    for log_entry in logs[:3]:
                        ts = log_entry.get("timestamp", "?")
                        level = log_entry.get("level", "?")
                        msg = str(log_entry.get("message", ""))[:120]
                        trace_id = log_entry.get("trace_id", "")
                        parts.append(
                            f"- [{level}] {ts}: {msg}"
                            + (f" (trace_id={trace_id})" if trace_id else "")
                        )

                analysis = prefetch.get("analysis", {})
                error_spans = analysis.get("error_spans", [])
                if error_spans:
                    parts.append("\n### 错误 Span")
                    for span in error_spans[:3]:
                        name = span.get("operation_name", span.get("name", "?"))
                        dur = span.get("duration_ms", 0)
                        parts.append(f"- {name} (duration={dur}ms)")

                parts.append(
                    "\n（可基于以上数据诊断，也可进一步调 code_search/get_file_content 确认代码）"
                )
                break

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

    # Estimate tokens (rough heuristic: ~4 chars per token)
    total_chars = sum(len(str(m.content)) for m in messages if hasattr(m, "content"))
    estimated_tokens = total_chars // 4

    now = datetime.now(UTC)
    elapsed = (now - budget.started_at).total_seconds() if budget.started_at else 0.0

    return BudgetState(
        total_tokens=budget.total_tokens + estimated_tokens,
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

    Uses the UnifiedAgent ReAct agent with all 5 tools to diagnose any
    Web app bug type. Replaces the V2 multi-specialist fan-out.

    Args:
        state: Current DoctorState (after Ingest).

    Returns:
        Dict with report, findings, budget, early_stopped for state merge.
    """
    from src.graph.subgraphs.unified_agent import get_unified_agent

    evidence: NormalizedEvidence = state.evidence

    # ── 实时查询：从 Loki/Tempo 自动获取日志和 Trace ──────────────
    # 新架构不再接收预收集证据，Doctor 始终现场查询可观测性数据。
    if evidence.trigger_time:
        logger.info("auto_prefetch_observability", trigger_time=evidence.trigger_time)
        try:
            from datetime import datetime, timedelta

            from src.graph.state import Signal
            from src.tools.observability_unified import search_observability

            tt = datetime.fromisoformat(evidence.trigger_time)
            start = (tt - timedelta(minutes=5)).isoformat()
            end = (tt + timedelta(minutes=5)).isoformat()

            prefetch_result = await search_observability(
                source="auto",
                query='{service_name=~"demo-backend"}',
                start=start,
                end=end,
                analysis="errors",
                limit=50,
            )
            import json

            prefetch_data = json.loads(prefetch_result)
            log_count = len(prefetch_data.get("logs", []))
            trace_count = len(prefetch_data.get("traces", []))

            logger.info("auto_prefetch_done", logs=log_count, traces=trace_count)

            if log_count > 0 or trace_count > 0:
                prefetch_signal = Signal(
                    signal_id="sig-auto-prefetch",
                    source="trace" if trace_count > 0 else "log",
                    signal_type="error_span" if trace_count > 0 else "error_log",
                    service_tier="backend",
                    severity="error",
                    summary=(
                        f"实时查询：Loki={log_count}条日志, Tempo={trace_count}条Trace。"
                        f"详情见下方【预查询原始数据】。"
                    ),
                    metadata={"prefetch_data": prefetch_data},
                )
                evidence.golden_signals.append(prefetch_signal)
        except Exception as exc:
            logger.warning("auto_prefetch_failed", error=str(exc))

    # Format evidence for the agent
    evidence_text = format_evidence_for_agent(evidence)

    logger.info(
        "unified_agent_invoking",
        case_id=state.case_id,
        signal_count=len(evidence.golden_signals),
        correlation_count=len(evidence.correlations),
    )

    # Invoke the ReAct agent
    try:
        agent = get_unified_agent()

        # ── Langfuse LLM tracing (graceful degradation) ──────────────
        invoke_config: dict[str, Any] = {}
        langfuse_handler = None
        try:
            from src.observability.langfuse_tracing import get_langfuse_handler

            langfuse_handler = get_langfuse_handler()
            invoke_config["callbacks"] = [langfuse_handler]
            # Manually start trace — on_chain_start doesn't fire in
            # LangGraph node context
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

        agent_result = await agent.ainvoke(
            {"messages": [HumanMessage(content=evidence_text)]},
            config=invoke_config if invoke_config else None,  # type: ignore[arg-type]
        )

        # Finalize Langfuse trace
        if langfuse_handler is not None:
            try:
                langfuse_handler.end_trace(
                    output_data={"result": str(agent_result)[:500]},
                )
            except Exception as lf_exc:
                logger.debug(
                    "langfuse_end_trace_error",
                    case_id=state.case_id,
                    error=str(lf_exc),
                )
    except Exception as exc:
        logger.error("unified_agent_exception", error=str(exc), case_id=state.case_id)
        return handle_agent_failure(state, exc)

    # Parse outputs
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
