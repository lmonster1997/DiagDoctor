"""
CopilotKit diagnosis graph - BugInfo -> DiagnosisAgent -> (HITL) -> END.

4-node pipeline with two Human-In-The-Loop (HITL) resume branches:

    START -> bug_info -> diagnosis_agent -> [route_after_diagnosis]
                                                 |
                         clarification_requested & count<MAX -> clarify_input (interrupt, P1)
                         early_stopped & !hitl_resumed        -> human_input (interrupt, #5)
                         else                                  -> END
                                                 |
                         clarify resume w/ answer   -> diagnosis_agent (pass 2) -> END
                         clarify resume w/ empty    -> END (accept current report)
                         human resume w/ guidance   -> diagnosis_agent (pass 2) -> END
                         human resume w/ empty      -> END (accept current report)

**BugInfo node**: parses the user's free-text chat message, extracts
structured bug info (description, trigger_time, trace_ids), auto-prefetches
logs+traces from Loki/Tempo, and normalizes them into ``NormalizedEvidence``.

**DiagnosisAgent node**: consumes the normalized evidence identically to
the REST API path - formats it into a system+human message pair, invokes
the ``create_agent`` subgraph (with all 8 middlewares), and produces a
structured diagnosis report. On HITL resume (``human_guidance`` for budget
exhaustion, or ``clarification_answer`` for P1 active clarification), it
builds a *continuation* message set (prior findings summary + the operator
input) and runs an informed second ReAct pass with a fresh budget.

**HumanInput node**: the #5 HITL interrupt point. When budget exhausts
before convergence (``early_stopped``), the graph pauses here via
``interrupt()`` and waits for one operator guidance line. Resume with
``Command(resume=<guidance>)`` (same ``thread_id``) - non-empty guidance
re-enters ``diagnosis_agent`` for a second pass; empty guidance accepts
the current best-effort report. One-shot: ``hitl_resumed`` gates a single
HITL cycle (a second exhaustion routes straight to END).

**ClarifyInput node**: the P1 active-clarification interrupt point. When the
agent *proactively* calls ``request_user_clarification`` (it realises it
lacks info tools can't fetch -- intermittent? recent change? caller?),
``ClarificationMiddleware`` stops the inner ReAct loop and the node routes
here to ``interrupt()`` with the agent's question. Resume with
``Command(resume=<answer>)`` - non-empty answer re-enters ``diagnosis_agent``
for an informed continuation pass; empty accepts the current best-effort.
Bounded by ``clarification_count < MAX_CLARIFICATIONS`` (not unlimited).

State schema: typed ``DoctorState`` (TypedDict) so the declared ``add``
reducers on findings/budget_ticks/total_cost actually run, and
``messages`` uses ``add_messages`` so the chat history persists across the
pause/resume boundary. Compiled with a persistent SQLite checkpointer
(``_LazyAsyncSqliteSaver`` -> ``data/checkpoints.db``) so a paused diagnosis
survives process restarts and can be resumed later. See state.py +
checkpointer.py + ``docs/followup-plan-20260715.md`` #5/#7.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage

from src.config import settings
from src.engine.agent import (
    _build_system_prompt,
    get_diagnosis_agent,
)
from src.engine.budget.constants import MAX_CLARIFICATIONS, RECURSION_LIMIT
from src.engine.nodes.bug_info import bug_info_node
from src.engine.run_context import DiagnosisRunContext
from src.engine.state import DoctorState, NormalizedEvidence
from src.memory.long_term.case_retriever import (
    format_similar_cases,
    search_historical_cases,
)
from src.observability.logger import bind_log_context, clear_log_context, get_logger

logger = get_logger(__name__)


def _filter_visible_messages(messages: list[Any]) -> list[Any]:
    """Strip SystemMessage and HumanMessage from agent output.

    CopilotKit streams ALL messages back to the frontend.  The
    SystemMessage (LLM instructions) and HumanMessage (evidence
    dump + signal data) are internal context — they must never
    appear in the user-visible chat.
    """
    from langchain_core.messages import AIMessage, ToolMessage

    return [m for m in messages if isinstance(m, (AIMessage, ToolMessage))]


def _format_scratchpad(findings: list[Any]) -> str:
    """§7.2: render prior findings as a 3-section hypothesis tree for续查 injection.

    Groups by ``Finding.status`` into 已确认事实(✓) / 已排除假设(✗) / 待验证线索(?),
    each line carrying evidence or the counterexample that excluded it. Empty
    sections omitted. This is the L4 hypothesis-tree structure (minus the LLM
    summary compression, which §5.4 rejects) -- it surfaces "what's been ruled
    out" so the resumed agent doesn't re-walk dead branches (the FE-021 flail
    root cause, §5.3 理由 2).

    Falls back to "(暂无)" when there are no usable findings.
    """
    confirmed = [f for f in findings if getattr(f, "status", None) == "confirmed"]
    excluded = [f for f in findings if getattr(f, "status", None) == "excluded"]
    pending = [f for f in findings if getattr(f, "status", None) == "pending"]

    sections: list[str] = []
    if confirmed:
        lines = "\n".join(
            f"- [✓] {f.summary} | 证据: {', '.join(f.evidence_refs) or '无'}"
            for f in confirmed
            if getattr(f, "summary", "")
        )
        if lines:
            sections.append(f"## 已确认事实(可依赖)\n{lines}")
    if excluded:
        lines = "\n".join(
            f"- [✗] {f.summary} | 反例: {getattr(f, 'refutation_evidence', '') or '未记录'}"
            for f in excluded
            if getattr(f, "summary", "")
        )
        if lines:
            sections.append(f"## 已排除假设(别再试)\n{lines}")
    if pending:
        lines = "\n".join(
            f"- [?] {f.summary} | 证据: {', '.join(f.evidence_refs) or '无'}"
            for f in pending
            if getattr(f, "summary", "")
        )
        if lines:
            sections.append(f"## 待验证线索(重点查)\n{lines}")

    return "\n\n".join(sections) if sections else "(暂无)"


async def _build_similar_cases_message(
    state: DoctorState, evidence: NormalizedEvidence, is_resume: bool
) -> tuple[HumanMessage | None, dict[str, Any]]:
    """Retrieve (or reuse cached) similar historical cases as a HumanMessage.

    First pass (``not is_resume``): query ``search_historical_cases`` and format
    via §6.5; cache the text + case_ids in state for the resume pass. Resume:
    reuse the cached ``similar_cases_text`` (no re-query). Design §6.5: "only
    first pass" means don't re-QUERY, not don't re-inject -- the inner agent is
    a fresh ``ainvoke`` per pass, so pass-1's injection would otherwise be lost
    on resume.

    Gated by ``settings.rag_injection_enabled``. Graceful degradation: any
    failure or empty recall -> ``(None, {})`` so diagnosis proceeds without RAG.

    Returns ``(message_or_None, state_updates)``; ``state_updates`` carries
    ``retrieved_case_ids`` + ``similar_cases_text`` on first pass (empty dict on
    resume, preserving pass-1's cached values).
    """
    if not settings.rag_injection_enabled:
        return None, {}

    if is_resume:
        cached = state.get("similar_cases_text") or ""
        if not cached:
            return None, {}
        return HumanMessage(content=cached), {}

    state_updates: dict[str, Any] = {"retrieved_case_ids": [], "similar_cases_text": ""}
    try:
        scored = await search_historical_cases(evidence)
    except Exception:
        logger.warning("rag_injection_failed", exc_info=True)
        return None, state_updates
    if not scored:
        return None, state_updates

    text = format_similar_cases(scored)
    if not text:
        return None, state_updates

    state_updates["retrieved_case_ids"] = [c.case_id for c in scored]
    state_updates["similar_cases_text"] = text
    return HumanMessage(content=text), state_updates


# ═════════════════════════════════════════════════════════════════════
# DiagnosisAgent node (adapted for CopilotKit state)
# ═════════════════════════════════════════════════════════════════════


async def _diagnosis_agent_node(state: DoctorState) -> dict[str, Any]:
    """CopilotKit-adapted diagnosis agent node.

    Reads ``state["evidence"]`` (NormalizedEvidence produced by BugInfoNode),
    formats it into messages, invokes the ``create_agent`` subgraph, and
    returns the diagnosis report + findings.

    Mirrors ``diagnosis_agent_node`` from ``nodes/diagnosis_agent/node.py``
    but operates on the typed ``DoctorState`` (TypedDict) graph schema.
    """
    import contextlib

    from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

    from src.evidence.formatter import format_evidence_for_agent

    evidence: NormalizedEvidence | None = state.get("evidence")
    if evidence is None:
        logger.warning("copilotkit_diag_no_evidence")
        return {"report": None, "findings": []}

    # ── Mirror diagnosis_agent_node: narrow search_observability
    #     default window to trigger_time ± 5min (per-case isolation).
    #     Without this, the tool defaults to "last 1 hour" and floods
    #     the agent with irrelevant noise → agent flails → 22+ tool
    #     calls → budget exhaustion.
    try:
        from src.tools.observability_unified import set_trigger_time

        set_trigger_time(evidence.trigger_time)
    except (ImportError, AttributeError):
        pass

    case_id = state.get("case_id") or ""
    evidence_text = format_evidence_for_agent(evidence)

    # Bind log correlation early so the head-of-node logs below
    # (copilotkit_diagnosis_agent_invoking, langfuse setup) carry trace_id.
    # trace_id = case_id (the Langfuse trace id is created lazily inside ainvoke
    # and doesn't propagate back to the log contextvar across langgraph's async
    # boundary, so it can't be used as the log join key). case_id is ALSO bound
    # as a STABLE field: trace_id collides with Tempo trace_ids on tool logs
    # (querying_tempo_trace passes trace_id=<tempo tid>), so case_id is the
    # reliable per-diagnosis join key. Cleared on every exit path below.
    _lf_session_id = state.get("langfuse_session_id")
    bind_log_context(
        trace_id=case_id or "",
        session_id=_lf_session_id or "",
        case_id=case_id or "",
    )

    logger.info(
        "copilotkit_diagnosis_agent_invoking",
        case_id=case_id,
        signal_count=len(evidence.golden_signals),
        correlation_count=len(evidence.correlations),
    )

    base_prompt = _build_system_prompt()

    # ── #5 HITL resume: if resuming with operator guidance, build a
    #     continuation message set (prior findings summary + guidance)
    #     instead of the fresh initial pair. The inner agent runs an
    #     informed second ReAct pass with a fresh budget (a new
    #     DiagnosisRunContext is constructed below). ──
    # P1: a clarification resume (agent proactively asked, user answered) is
    # also a continuation pass -- ``clarification_answer`` carries the user's
    # answer. Both are "informed second pass"; the continuation text branches
    # below. ``clarification_answer`` is cleared in this node's return so a
    # later budget-HITL resume doesn't misread a stale answer.
    human_guidance = state.get("human_guidance")
    clarification_answer = state.get("clarification_answer")
    guidance_str = str(human_guidance).strip() if human_guidance else ""
    answer_str = str(clarification_answer).strip() if clarification_answer else ""
    is_resume = bool(guidance_str or answer_str)

    # ── #1 RAG: retrieve similar historical cases (design §6.5). First
    #    pass queries + caches; resume re-injects the cached block without
    #    re-querying. Graceful degradation: None on failure / empty recall /
    #    disabled -> diagnosis proceeds without historical reference. ──
    similar_msg, rag_updates = await _build_similar_cases_message(state, evidence, is_resume)

    if is_resume:
        prior_findings = state.get("findings", []) or []
        scratchpad = _format_scratchpad(prior_findings)
        if answer_str:
            # P1 clarification resume: agent asked, user answered.
            question = state.get("clarification_question") or ""
            continuation = (
                "【澄清续查】你上一轮主动向用户提了澄清问题。\n\n"
                f"你的问题: {question}\n"
                f"用户回答: {answer_str}\n\n"
                f"{scratchpad}\n\n"
                "请结合用户的回答继续调查(别重复已排除的假设),并输出最终诊断报告 JSON。"
            )
            logger.info(
                "copilotkit_diag_clarification_resume_pass",
                case_id=case_id,
                prior_findings=len(prior_findings),
            )
        else:
            # #5 budget-exhaustion resume: operator steering hint.
            continuation = (
                "【续查模式】上一轮调查因预算耗尽未收敛。\n\n"
                f"{scratchpad}\n\n"
                f"操作员补充引导: {guidance_str}\n\n"
                "请基于已有发现和引导继续调查(别重复已排除的假设),并输出最终诊断报告 JSON。"
            )
            logger.info(
                "copilotkit_diag_hitl_resume_pass",
                case_id=case_id,
                prior_findings=len(prior_findings),
            )
        initial_messages: list[BaseMessage] = [SystemMessage(content=base_prompt)]
        if similar_msg is not None:
            initial_messages.append(similar_msg)
        initial_messages.append(HumanMessage(content=evidence_text))
        initial_messages.append(HumanMessage(content=continuation))
    else:
        initial_messages = [SystemMessage(content=base_prompt)]
        if similar_msg is not None:
            initial_messages.append(similar_msg)
        initial_messages.append(HumanMessage(content=evidence_text))

    # ── Langfuse setup (graceful degradation) ────────────────────
    # Trace lifecycle (start_trace/end_trace) is owned by the NODE, not
    # LangfuseTracingMiddleware: the handler is a process-wide shared
    # singleton (get_langfuse_handler caches it), so the per-diagnosis
    # trace_id/session_id can't be baked in at construction -- they're set
    # explicitly here. The middleware only records tool spans
    # (awrap_tool_call); end_trace runs in _finalize_langfuse_trace below.
    _lf_trace_id = state.get("langfuse_trace_id")
    langfuse_handler = None
    try:
        langfuse_handler = _get_langfuse_handler_for_dict_state(case_id, evidence_text)
        if langfuse_handler is not None:
            langfuse_handler.prepare_for_managed_trace()
            if _lf_session_id:
                langfuse_handler.set_external_session_id(_lf_session_id)
            langfuse_handler.start_trace(
                input_data={"evidence": evidence_text[:500]},
                trace_id=_lf_trace_id,
            )
    except Exception:
        # Langfuse outage must not block diagnosis (graceful degradation).
        pass

    logger.info(
        "copilotkit_diag_langfuse_trace_id",
        case_id=case_id,
        langfuse_trace_id=_lf_trace_id,
        langfuse_session_id=_lf_session_id,
        has_handler=langfuse_handler is not None,
    )

    # Attach Langfuse handler at agent.ainvoke level (NOT model.with_config).
    # Tool callbacks (on_tool_start/end) are no-ops, so no double-recording.
    invoke_config: dict[str, Any] = {"recursion_limit": RECURSION_LIMIT}
    if langfuse_handler is not None:
        invoke_config["callbacks"] = [langfuse_handler]

    run_ctx = DiagnosisRunContext(
        case_id=case_id or "",
        langfuse_handler=langfuse_handler,
        system_prompt_text=base_prompt,
        evidence_text=evidence_text,
    )
    # Log context was bound at the top of this node (trace_id=case_id,
    # upgraded to the Langfuse trace id by LangfuseTracing middleware during
    # the ainvoke below). Cleared on every exit path so finalize-phase logs
    # (diagnosis_report_parsed / langfuse_trace_ended) still carry trace_id.
    # run_ctx is passed as ``context=`` to ainvoke below; middlewares mutate
    # it in place via runtime.context, and the local ref here reads those
    # mutations back after ainvoke returns.
    try:
        agent = get_diagnosis_agent()
        result = await agent.ainvoke(  # type: ignore[call-overload]
            {"messages": initial_messages},
            config=invoke_config,
            context=run_ctx,
        )
        final_messages: list[BaseMessage] = result.get("messages", [])
    except Exception as exc:
        logger.error("copilotkit_diag_exception", error=str(exc), case_id=case_id)
        if langfuse_handler is not None:
            with contextlib.suppress(Exception):
                langfuse_handler.end_trace(output_data={"error": str(exc)})
        clear_log_context()
        return {
            "report": None,
            "findings": [],
            "clarification_requested": False,
            "clarification_answer": None,
        }

    budget_exhausted = run_ctx.budget_exhausted
    forced_call_triggered = run_ctx.forced_call_triggered
    # P1: ClarificationMiddleware sets this ( + clarification_question) when the
    # agent called ``request_user_clarification`` this pass, then jumped to end.
    # Routed to the ``clarify_input`` interrupt node; the best-effort fallback
    # report below is kept in case the operator later declines to answer.
    clarification_requested = run_ctx.clarification_requested
    clarification_question = run_ctx.clarification_question

    # Build a minimal budget_state for _finalize_report
    from src.engine.state import BudgetState

    budget_state = BudgetState()

    # §8.1 path 2: the retrieved set used to clamp referenced_case_ids. Pass 1
    # just fetched it (in rag_updates); resume reuses pass-1's cache (in state).
    # RAG off / empty recall / retrieval failure -> empty -> referenced forced
    # empty (the agent can't cite cases it never saw).
    if "retrieved_case_ids" in rag_updates:
        effective_retrieved = rag_updates["retrieved_case_ids"]
    else:
        effective_retrieved = list(state.get("retrieved_case_ids") or [])

    report, findings, budget_state, early_stopped = _finalize_report_for_dict_state(
        final_messages, budget_exhausted, effective_retrieved
    )

    _finalize_langfuse_trace(
        langfuse_handler=langfuse_handler,
        report=report,
        early_stopped=early_stopped,
        budget_state=budget_state,
        forced_call_triggered=forced_call_triggered,
        case_id=case_id or "",
    )

    clear_log_context()
    return {
        "messages": _filter_visible_messages(final_messages),
        "report": report,
        "findings": findings,
        "budget": budget_state,
        "early_stopped": early_stopped,
        "clarification_requested": clarification_requested,
        "clarification_question": clarification_question or None,
        # Clear the consumed answer so a later budget-HITL resume pass doesn't
        # misread a stale clarification_answer as a clarification resume.
        "clarification_answer": None,
        **rag_updates,
    }


def _get_langfuse_handler_for_dict_state(case_id: str, evidence_text: str) -> Any | None:
    """Get Langfuse handler (mirrors _get_langfuse_handler for dict state)."""
    try:
        from src.observability.langfuse_tracing import get_langfuse_handler

        return get_langfuse_handler()
    except (ValueError, ImportError):
        return None


def _best_finding_for_root_cause(findings: list[Any]) -> Any | None:
    """Pick the best finding as a root_cause fallback when no JSON report was emitted.

    Preference: confirmed (a root_cause hypothesis the agent validated) > pending
    (an unverified lead, but still a concrete hypothesis) > any. Excluded findings
    are skipped -- they're ruled-out dead ends, not a root cause.
    """
    for status in ("confirmed", "pending"):
        for f in findings:
            if getattr(f, "status", None) == status:
                return f
    return findings[0] if findings else None


def _finalize_report_for_dict_state(
    messages: list[Any], budget_exhausted: bool, retrieved_case_ids: list[str] | None = None
) -> tuple[Any, list[Any], Any, bool]:
    """Parse messages into report + findings (mirrors _finalize_report from node.py)."""
    from src.engine.budget.tracker import update_budget
    from src.engine.parsing import (
        clamp_referenced_case_ids,
        extract_findings,
        parse_diagnosis_report,
    )
    from src.engine.state import BudgetState, DiagnosisReport

    agent_result: dict[str, Any] = {"messages": messages}
    report = parse_diagnosis_report(agent_result)
    findings = extract_findings(agent_result)
    # budget_state is telemetry-only (Langfuse tool_calls/tokens/elapsed).
    # early_stopped reads the AUTHORITATIVE runtime ``budget_exhausted`` flag
    # set by BudgetGuardMiddleware (model_call_count > MAX_MODEL_CALLS / token
    # / time caps), captured during the loop before the forced call. We do NOT
    # re-derive it from messages: tool_calls (sum of tool invocations) !=
    # model_call_count (LLM calls), so a message-based proxy (the old
    # is_budget_exceeded) false-positived on parallel-tool CONVERGED runs ->
    # spurious HITL. §6.1 split-brain: gate on the runtime signal, not a
    # re-derivation with a different口径.
    budget_state = update_budget(BudgetState(), agent_result)
    early_stopped = budget_exhausted

    if report is None:
        # Agent 未输出有效 JSON(被截断 + forced call 失败)。从 findings 里挑最佳
        # 根因候选:confirmed > pending > 任意。远好过旧实现拿最后一条 AIMessage
        # 的推理文本当 root_cause(那是调查中的碎碎念,不是结论)。
        best = _best_finding_for_root_cause(findings)
        report = DiagnosisReport(
            primary_category="",
            root_cause=best.summary if best else "诊断未完成",
            confidence=0.3,
            early_stopped=early_stopped,
            notes="Agent 未输出有效 JSON，根因取自假设记录" if best else "Agent 未输出有效 JSON",
        )

    if early_stopped:
        report.early_stopped = True
        if not report.notes:
            report.notes = "预算超限，提前终止诊断"

    # §8.1 path 2: clamp the agent's declared referenced_case_ids to the cases
    # actually retrieved this run (anti-hallucination -- the agent can only
    # cite cases it was shown in the §6.5 injection block). Fail-closed: when
    # retrieved is unknown/empty, referenced is forced empty. ``retrieved`` is
    # computed by the caller (pass 1: just-fetched; resume: pass-1 cache).
    report.referenced_case_ids = clamp_referenced_case_ids(
        report.referenced_case_ids, retrieved_case_ids or []
    )

    return report, findings, budget_state, early_stopped


def _finalize_langfuse_trace(
    langfuse_handler: Any | None,
    report: Any,
    early_stopped: bool,
    budget_state: Any,
    forced_call_triggered: bool,
    case_id: str,
) -> None:
    """End the Langfuse trace with the report + run flags. Errors are non-fatal."""
    if langfuse_handler is None:
        return
    try:
        report_dict = report.model_dump(mode="json") if hasattr(report, "model_dump") else {}
        langfuse_handler.end_trace(
            output_data={
                "diagnosis_report": report_dict,
                "early_stopped": early_stopped,
                "tool_calls": budget_state.tool_calls,
                "total_tokens": budget_state.total_tokens,
                "elapsed_seconds": budget_state.elapsed_seconds,
                "forced_final_json_call": forced_call_triggered,
            },
        )
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════
# Graph construction
# ═════════════════════════════════════════════════════════════════════

# ═════════════════════════════════════════════════════════════════════
# #5 HITL - HumanInput node + routing
# ═════════════════════════════════════════════════════════════════════


async def human_input_node(state: DoctorState) -> dict[str, Any]:
    """HITL interrupt point: pause for one operator guidance line.

    Reached when ``diagnosis_agent`` exhausted its budget before converging
    (``early_stopped``) and no HITL cycle has run yet (``not hitl_resumed``).
    Pauses the graph via ``interrupt()``; the checkpoint is persisted so the
    diagnosis can be resumed later (even after a process restart) by
    re-invoking the graph with ``Command(resume=<guidance>)`` on the same
    ``thread_id``.

    Resume semantics:
    - non-empty guidance -> re-enter ``diagnosis_agent`` for an informed
      second pass (operator steers the investigation).
    - empty guidance -> accept the current best-effort (early_stopped) report
      and end (operator declines to steer).

    ``hitl_resumed`` is set either way so a subsequent budget exhaustion
    routes straight to END (one-shot HITL, no infinite pause loop).
    """
    from langgraph.types import interrupt

    prior_findings = state.get("findings", []) or []
    prior_summary = "; ".join(f.summary for f in prior_findings if getattr(f, "summary", ""))[:500]
    excluded_count = sum(1 for f in prior_findings if getattr(f, "status", None) == "excluded")
    prompt = (
        "预算耗尽,诊断未收敛。"
        f"已收集 {len(prior_findings)} 条发现"
        f"{f'(已排除 {excluded_count} 个假设)' if excluded_count else ''}"
        f"{'(' + prior_summary + ')' if prior_summary else ''}。"
        "请补充一句人工引导(如可疑方向/已知线索),agent 将据此续查;"
        "留空则直接采纳当前 best-effort 结论。"
    )
    guidance = interrupt(
        {
            "type": "hitl_guidance_request",
            "prompt": prompt,
            "prior_findings_count": len(prior_findings),
            "early_stopped": True,
        }
    )
    guidance_str = str(guidance).strip() if guidance else ""
    logger.info(
        "copilotkit_hitl_human_input_resumed",
        case_id=state.get("case_id") or "",
        guidance_len=len(guidance_str),
        accept_current=not guidance_str,
    )
    return {"human_guidance": guidance_str or None, "hitl_resumed": True}


def _route_after_diagnosis(state: DoctorState) -> str:
    """Route after diagnosis_agent: pause for clarification or budget-HITL, else END.

    Priority:
    1. P1 active clarification -- the agent proactively called
       ``request_user_clarification`` (``clarification_requested``), bounded by
       ``clarification_count < MAX_CLARIFICATIONS``. Once the cap is hit, a
       further request is dropped (accept current best-effort) so the agent
       can't ask indefinitely (§2.1).
    2. #5 budget-exhaustion HITL -- ``early_stopped`` and not yet resumed.
    3. otherwise (converged, or already resumed) -> ``end``.

    One-shot: once ``hitl_resumed`` is True, a second budget exhaustion goes
    straight to END instead of re-pausing. Clarification has its own bound
    (``clarification_count``), independent of the budget-HITL one-shot gate.
    """
    clarification_requested = bool(state.get("clarification_requested"))
    clarification_count = int(state.get("clarification_count") or 0)
    if clarification_requested and clarification_count < MAX_CLARIFICATIONS:
        return "clarify_input"
    early_stopped = bool(state.get("early_stopped"))
    hitl_resumed = bool(state.get("hitl_resumed"))
    if early_stopped and not hitl_resumed:
        return "human_input"
    return "end"


def _route_after_human_input(state: DoctorState) -> str:
    """Route after human_input: re-investigate only if guidance was provided."""
    if state.get("human_guidance"):
        return "diagnosis_agent"
    return "end"


async def clarify_input_node(state: DoctorState) -> dict[str, Any]:
    """P1 active-clarification interrupt point: pause for the user's answer.

    Reached when ``diagnosis_agent`` proactively called
    ``request_user_clarification`` (signalled via ``clarification_requested``,
    set by ``ClarificationMiddleware`` + read back by the node). Mirrors
    ``human_input_node`` but for an agent-initiated question rather than budget
    exhaustion: the agent's question is carried in ``clarification_question`` and
    surfaced to the operator; resume with ``Command(resume=<answer>)``.

    Resume semantics:
    - non-empty answer -> ``clarification_answer`` set -> re-enter
      ``diagnosis_agent`` for an informed continuation pass (the answer is
      injected as context alongside the prior-findings scratchpad).
    - empty answer -> operator declines -> END (accept current best-effort).

    ``clarification_count`` is bumped either way; ``_route_after_diagnosis``
    gates on ``clarification_count < MAX_CLARIFICATIONS`` (bounded, not
    unlimited). ``clarification_requested`` is cleared so a pass that doesn't
    re-ask routes normally.
    """
    from langgraph.types import interrupt

    question = state.get("clarification_question") or ""
    prior_findings = state.get("findings", []) or []
    answer = interrupt(
        {
            "type": "clarify",
            "question": question,
            "prior_findings_count": len(prior_findings),
            "early_stopped": bool(state.get("early_stopped")),
        }
    )
    answer_str = str(answer).strip() if answer else ""
    logger.info(
        "copilotkit_clarify_input_resumed",
        case_id=state.get("case_id") or "",
        answer_len=len(answer_str),
        declined=not answer_str,
    )
    return {
        "clarification_answer": answer_str or None,
        "clarification_count": int(state.get("clarification_count") or 0) + 1,
        "clarification_requested": False,
    }


def _route_after_clarify_input(state: DoctorState) -> str:
    """Route after clarify_input: re-investigate only if the user answered."""
    if state.get("clarification_answer"):
        return "diagnosis_agent"
    return "end"


_copilotkit_graph_instance: Any = None


def build_copilotkit_graph(checkpointer: Any = None) -> Any:
    """Build the CopilotKit diagnosis graph.

    4-node pipeline with two HITL resume branches (see module docstring):
    #5 budget-exhaustion (``human_input``) + P1 active clarification
    (``clarify_input``).

    Args:
        checkpointer: optional ``BaseCheckpointSaver``. Defaults to the
            persistent ``_LazyAsyncSqliteSaver`` (``data/checkpoints.db``).
            Tests pass an isolated temp-file saver to avoid cross-test
            checkpoint leakage.
    """
    from langgraph.graph import END, StateGraph

    from src.engine.checkpointer import make_checkpointer
    from src.engine.state import DoctorState

    builder = StateGraph(DoctorState)

    builder.add_node("bug_info", bug_info_node)
    builder.add_node("diagnosis_agent", _diagnosis_agent_node)
    builder.add_node("human_input", human_input_node)
    builder.add_node("clarify_input", clarify_input_node)

    builder.set_entry_point("bug_info")
    builder.add_edge("bug_info", "diagnosis_agent")
    # After diagnosis: P1 active clarification takes priority (agent proactively
    # asked), then #5 budget-exhaustion HITL, else END. See _route_after_diagnosis.
    builder.add_conditional_edges(
        "diagnosis_agent",
        _route_after_diagnosis,
        {
            "human_input": "human_input",
            "clarify_input": "clarify_input",
            "end": END,
        },
    )
    # #5 budget HITL: empty guidance -> END (accept current); non-empty ->
    # diagnosis_agent second pass.
    builder.add_conditional_edges(
        "human_input",
        _route_after_human_input,
        {"diagnosis_agent": "diagnosis_agent", "end": END},
    )
    # P1 clarification: empty answer -> END (accept current); non-empty ->
    # diagnosis_agent informed continuation pass.
    builder.add_conditional_edges(
        "clarify_input",
        _route_after_clarify_input,
        {"diagnosis_agent": "diagnosis_agent", "end": END},
    )

    return builder.compile(checkpointer=checkpointer or make_checkpointer())


def get_copilotkit_graph() -> Any:
    """Get or create the cached CopilotKit diagnosis graph."""
    global _copilotkit_graph_instance
    if _copilotkit_graph_instance is None:
        _copilotkit_graph_instance = build_copilotkit_graph()
    return _copilotkit_graph_instance


def generate_thread_id() -> str:
    """Generate a unique thread_id for a new diagnosis session."""
    import uuid

    return f"diag-{uuid.uuid4().hex[:12]}"
