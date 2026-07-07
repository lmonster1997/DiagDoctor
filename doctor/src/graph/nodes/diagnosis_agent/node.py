"""DiagnosisAgent LangGraph node — the V3 ReAct agent wrapped as a graph node.

This module owns only the node spine + 4 node-private helpers:
- ``_setup_trigger_time`` — expose trigger_time to search_observability via ContextVar
- ``_setup_langfuse_tracing`` — start Langfuse trace (graceful degradation)
- ``_finalize_report`` — parse messages → DiagnosisReport + findings + budget_state
- ``_finalize_langfuse_trace`` — end_trace with report + flags

The heavy lifting lives in sibling modules (``react_loop``, ``forced_call``,
``evidence``, ``parsing``, ``budget``, ``failure``). This file is intentionally
~45 lines of spine so the orchestration is readable end-to-end.

Import layout (matters for test monkeypatch):
- ``_llm_factory.get_llm_for_role`` is looked up via module attribute so
  ``monkeypatch.setattr(src.llm_factory, "get_llm_for_role", ...)`` takes effect.
- ``_build_system_prompt`` / ``get_all_tools`` are non-defensive imports at top.
- ``get_langfuse_handler`` / ``set_trigger_time`` stay as inline imports inside
  their helpers (defensive try/except) so the test's inline monkeypatch on
  ``src.observability.langfuse_tracing.get_langfuse_handler`` is picked up at
  call time and Langfuse-unavailable environments degrade gracefully.
"""

from __future__ import annotations

import contextlib
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

import src.llm_factory as _llm_factory
from src.graph.context_engine import ContextBudget
from src.graph.nodes.diagnosis_agent.budget import is_budget_exceeded, update_budget
from src.graph.nodes.diagnosis_agent.evidence import format_evidence_for_agent
from src.graph.nodes.diagnosis_agent.failure import handle_agent_failure
from src.graph.nodes.diagnosis_agent.forced_call import _maybe_forced_final_json_call
from src.graph.nodes.diagnosis_agent.parsing import extract_findings, parse_diagnosis_report
from src.graph.nodes.diagnosis_agent.react_loop import _run_react_loop
from src.graph.state import DiagnosisReport, DoctorState, NormalizedEvidence
from src.graph.subgraphs.diagnosis_agent import _build_system_prompt
from src.observability.logger import get_logger
from src.observability.tracing import traced
from src.tools import get_all_tools

logger = get_logger(__name__)


def _setup_trigger_time(evidence: NormalizedEvidence) -> None:
    """Expose trigger_time to search_observability via ContextVar.

    Defaults the tool's query window to trigger_time ± 5min (per-case
    isolation) instead of "last 1 hour" (which in batch runs contains logs
    from other cases and pollutes the diagnosis). Defensive: if the tool
    module isn't importable in this environment, silently skip.
    """
    try:
        from src.tools.observability_unified import set_trigger_time

        set_trigger_time(evidence.trigger_time)
    except (ImportError, AttributeError):
        pass


def _setup_langfuse_tracing(
    state: DoctorState, evidence_text: str
) -> tuple[Any | None, dict[str, Any]]:
    """Start a Langfuse trace for this diagnosis run.

    Returns ``(handler, invoke_config)``. On any Langfuse-unavailable
    failure (ImportError / ValueError) returns ``(None, {})`` so the rest of
    the node runs without tracing — graceful degradation. Inline import so
    test monkeypatch on ``src.observability.langfuse_tracing`` is honored.
    """
    invoke_config: dict[str, Any] = {}
    try:
        from src.observability.langfuse_tracing import get_langfuse_handler

        handler = get_langfuse_handler()
        invoke_config["callbacks"] = [handler]
        handler.start_trace(
            input_data={"evidence": evidence_text[:500]},
            trace_id=state.langfuse_trace_id,
        )
        logger.debug(
            "langfuse_tracing_enabled",
            case_id=state.case_id,
            reused_trace_id=state.langfuse_trace_id is not None,
        )
        return handler, invoke_config
    except (ValueError, ImportError) as lf_exc:
        logger.debug(
            "langfuse_tracing_disabled",
            case_id=state.case_id,
            reason=str(lf_exc),
        )
        return None, invoke_config


def _finalize_report(
    state: DoctorState,
    messages: list[BaseMessage],
    budget_exhausted: bool,
) -> tuple[DiagnosisReport, list[Any], Any, bool]:
    """Parse messages into report + findings + budget_state + early_stopped flag.

    baseline: 不兜底，parse 失败就给空报告。``early_stopped`` is True if
    either ``is_budget_exceeded`` (hard cap crossed) or ``budget_exhausted``
    (loop ran to MAX_TOOL_CALLS).
    """
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

    return report, findings, budget_state, early_stopped


def _finalize_langfuse_trace(
    langfuse_handler: Any | None,
    report: DiagnosisReport,
    early_stopped: bool,
    budget_state: Any,
    forced_call_triggered: bool,
    case_id: str,
) -> None:
    """End the Langfuse trace with the report + run flags. Errors are non-fatal."""
    if langfuse_handler is None:
        return
    try:
        report_dict = (
            report.model_dump(mode="json") if hasattr(report, "model_dump") else {}
        )
        langfuse_handler.end_trace(
            output_data={
                "diagnosis_report": report_dict,
                "early_stopped": early_stopped,
                "tool_calls": budget_state.tool_calls,
                "forced_final_json_call": forced_call_triggered,
            },
        )
        logger.debug(
            "langfuse_trace_finalized",
            case_id=case_id,
            primary_category=report.primary_category,
            confidence=report.confidence,
            early_stopped=early_stopped,
        )
    except Exception as lf_exc:
        logger.debug(
            "langfuse_end_trace_error",
            case_id=case_id,
            error=str(lf_exc),
        )


@traced()
async def diagnosis_agent_node(state: DoctorState) -> dict[str, Any]:
    """
    LangGraph node: unified diagnosis — ingest 后的唯一步骤.

    BASELINE + Iteration 1（dev-harness-redesign 分支）：
    - 硬约束：MAX_TOOL_CALLS 迭代上限 / MAX_TOKENS_BUDGET / MAX_TIME_SECONDS
    - 不做收敛检测、不做 phase 推进、不做兜底合成
    - agent 自然 stop（无 tool_calls）→ 解析最后一条 AIMessage 为 JSON
    - agent 跑满迭代 / 触发硬约束 → 同样解析最后一条 AIMessage
    - **Iteration 1 新增**：loop 结束后若最后一条 AIMessage 不含可 parse 的 JSON，
      做一次额外 LLM call（**不 bind_tools**），prompt 强制「基于已收集证据输出
      DiagnosisReport JSON，不要再调任何工具」。覆盖两种已观测 failure mode：
        mode 1（cap + 末轮空 content）：给 agent 一次「只能输出 content」的机会
        mode 2（natural stop + narrative）：给 agent 一次「把叙事格式化成 JSON」的机会
      已含 JSON 的 healthy case 跳过此 call——不影响 baseline 11 个 healthy case。

    Args:
        state: Current DoctorState (after Ingest).

    Returns:
        Dict with report, findings, budget, early_stopped for state merge.
    """
    evidence: NormalizedEvidence = state.evidence
    _setup_trigger_time(evidence)
    evidence_text = format_evidence_for_agent(evidence)
    logger.info(
        "diagnosis_agent_invoking",
        case_id=state.case_id,
        signal_count=len(evidence.golden_signals),
        correlation_count=len(evidence.correlations),
    )

    base_prompt = _build_system_prompt()
    messages: list[BaseMessage] = [
        SystemMessage(content=base_prompt),
        HumanMessage(content=evidence_text),
    ]

    llm = _llm_factory.get_llm_for_role("diagnosis")
    tools = get_all_tools()
    tool_map = {t.name: t for t in tools}
    llm_with_tools = llm.bind_tools(tools)

    ctx_budget = ContextBudget()
    ctx_budget.add_system_prompt(base_prompt)
    ctx_budget.add_evidence(evidence_text)
    ctx_budget.start_timer()

    langfuse_handler, invoke_config = _setup_langfuse_tracing(state, evidence_text)

    try:
        budget_exhausted = await _run_react_loop(
            messages=messages,
            llm_with_tools=llm_with_tools,
            tool_map=tool_map,
            ctx_budget=ctx_budget,
            langfuse_handler=langfuse_handler,
            invoke_config=invoke_config,
            case_id=state.case_id,
        )
    except Exception as exc:
        logger.error("diagnosis_agent_exception", error=str(exc), case_id=state.case_id)
        if langfuse_handler is not None:
            with contextlib.suppress(Exception):
                langfuse_handler.end_trace(output_data={"error": str(exc)})
        return handle_agent_failure(state, exc)

    forced_call_triggered = await _maybe_forced_final_json_call(
        messages=messages,
        llm=llm,
        ctx_budget=ctx_budget,
        invoke_config=invoke_config,
        case_id=state.case_id,
        budget_exhausted=budget_exhausted,
        langfuse_handler=langfuse_handler,
    )

    report, findings, budget_state, early_stopped = _finalize_report(
        state=state, messages=messages, budget_exhausted=budget_exhausted
    )

    _finalize_langfuse_trace(
        langfuse_handler=langfuse_handler,
        report=report,
        early_stopped=early_stopped,
        budget_state=budget_state,
        forced_call_triggered=forced_call_triggered,
        case_id=state.case_id,
    )

    logger.info(
        "diagnosis_agent_completed",
        case_id=state.case_id,
        primary_category=report.primary_category,
        confidence=report.confidence,
        tool_calls=budget_state.tool_calls,
        early_stopped=early_stopped,
        forced_final_json_call=forced_call_triggered,
    )

    return {
        "report": report,
        "findings": findings,
        "budget": budget_state,
        "early_stopped": early_stopped,
    }
