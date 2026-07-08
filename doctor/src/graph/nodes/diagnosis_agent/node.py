"""DiagnosisAgent LangGraph node — wraps the create_agent + middleware graph.

V4 (this version) replaces the hand-written ReAct loop (V3, ``react_loop.py``)
with langchain ``create_agent`` + 5 middlewares (see
``subgraphs/diagnosis_agent.py:build_diagnosis_agent`` for the middleware
registration order rationale). This node is the DoctorState ↔ AgentState
adapter:

1. Format NormalizedEvidence → HumanMessage + SystemMessage (AgentState input)
2. Set ``DiagnosisRunContext`` into a ContextVar (per-invocation state shared
   with middlewares — case_id, langfuse_handler, system/evidence texts for
   initial token accounting)
3. ``agent.ainvoke({"messages": [...]}, config={callbacks, recursion_limit})``
4. Parse returned messages → DiagnosisReport + findings + budget_state
   (reuses V3's ``_finalize_report`` / ``parse_diagnosis_report`` unchanged)
5. ``end_trace`` with the parsed report (owned here, not in middleware —
   middleware's aafter_agent runs inside ainvoke before the report is parsed)

The 5 middlewares own the loop mechanics: Langfuse start_trace + tool spans,
BudgetGuard caps, ToolDedup/Truncation, ForcedFinalCall. See
``middleware/`` package docstrings for the case-driven rationale per middleware.

Import layout (matters for test monkeypatch):
- ``_llm_factory.get_llm_for_role`` looked up via module attribute so
  ``monkeypatch.setattr(src.llm_factory, "get_llm_for_role", ...)`` takes effect
  (ForcedFinalCallMiddleware imports ``src.llm_factory`` the same way).
- ``get_diagnosis_agent`` imported lazily inside the node to avoid a circular
  import at module load (subgraphs/diagnosis_agent → middleware → nodes pkg
  → this module → subgraphs/diagnosis_agent).
- ``get_langfuse_handler`` / ``set_trigger_time`` stay inline imports inside
  their helpers (defensive try/except) so Langfuse-unavailable environments
  degrade gracefully.
"""

from __future__ import annotations

import contextlib
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from src.graph.nodes.diagnosis_agent.budget import is_budget_exceeded, update_budget
from src.graph.nodes.diagnosis_agent.evidence import format_evidence_for_agent
from src.graph.nodes.diagnosis_agent.failure import handle_agent_failure
from src.graph.nodes.diagnosis_agent.middleware.run_context import (
    DiagnosisRunContext,
    clear_run_context,
    set_run_context,
)
from src.graph.nodes.diagnosis_agent.parsing import extract_findings, parse_diagnosis_report
from src.graph.state import DiagnosisReport, DoctorState, NormalizedEvidence
from src.observability.logger import get_logger
from src.observability.tracing import traced

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


def _get_langfuse_handler(state: DoctorState, evidence_text: str) -> Any | None:
    """Get the Langfuse callback handler (does NOT start_trace, does NOT build invoke_config).

    ``start_trace`` is owned by ``LangfuseTracingMiddleware.abefore_agent`` (it
    runs at invocation start inside ``agent.ainvoke`` and also initializes the
    ContextBudget there). The handler is attached to each LLM call alone by
    ``LangfuseTracingMiddleware.awrap_model_call`` (via ``model.with_config``)
    — NOT via top-level ``config={"callbacks": [...]}``, which would propagate
    to the ToolNode and double-record tool calls (see awrap_model_call docstring).

    Returns the handler, or ``None`` on Langfuse-unavailable failure
    (ImportError / ValueError) — graceful degradation, middlewares read
    ``ctx.langfuse_handler is None`` and skip record_* calls.
    """
    try:
        from src.observability.langfuse_tracing import get_langfuse_handler

        handler = get_langfuse_handler()
        logger.debug(
            "langfuse_handler_acquired",
            case_id=state.case_id,
            reused_trace_id=state.langfuse_trace_id is not None,
        )
        return handler
    except (ValueError, ImportError) as lf_exc:
        logger.debug(
            "langfuse_tracing_disabled",
            case_id=state.case_id,
            reason=str(lf_exc),
        )
        return None


def _finalize_report(
    state: DoctorState,
    messages: list[BaseMessage],
    budget_exhausted: bool,
) -> tuple[DiagnosisReport, list[Any], Any, bool]:
    """Parse messages into report + findings + budget_state + early_stopped flag.

    baseline: 不兜底，parse 失败就给空报告。``early_stopped`` is True if
    either ``is_budget_exceeded`` (hard cap crossed) or ``budget_exhausted``
    (loop ran to MAX_TOOL_CALLS / token / time cap via BudgetGuard jump_to).
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
        report_dict = report.model_dump(mode="json") if hasattr(report, "model_dump") else {}
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
    """LangGraph node: unified diagnosis via create_agent + 5 middlewares.

    DoctorState adapter around the create_agent subgraph:
    - Formats evidence + system prompt → AgentState messages
    - Sets DiagnosisRunContext (ContextVar) for middlewares to read/write
    - Invokes the create_agent graph (which runs the ReAct loop + middlewares:
      Langfuse start_trace, BudgetGuard caps, ToolDedup/Truncation,
      ForcedFinalCall post-loop JSON)
    - Parses returned messages → DiagnosisReport (reuses V3 parsing logic)
    - Ends the Langfuse trace with the parsed report

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

    # Lazy import to avoid circular import at module load (subgraphs module
    # imports middleware which lives under the nodes package that imports this
    # node). get_diagnosis_agent caches the built agent at module level.
    from src.graph.subgraphs.diagnosis_agent import _build_system_prompt, get_diagnosis_agent

    base_prompt = _build_system_prompt()
    initial_messages: list[BaseMessage] = [
        SystemMessage(content=base_prompt),
        HumanMessage(content=evidence_text),
    ]

    langfuse_handler = _get_langfuse_handler(state, evidence_text)
    # NOTE: the handler is NOT put in any top-level config callbacks. create_agent
    # propagates top-level config callbacks to BOTH the model node AND the ToolNode
    # (via the Runnable config contextvar), which double-records tool calls
    # (callback on_tool_start/end + middleware record_tool_span) and breaks
    # score_process_quality efficiency. Instead, LangfuseTracingMiddleware.
    # awrap_model_call attaches the handler to each LLM call alone via
    # model.with_config, so tools are recorded only by record_tool_span (single
    # source) — matching the hand-written loop's observability model.
    # langfuse_handler still goes into the run context for: middleware
    # start_trace / record_tool_span / record_tool_skipped, and
    # ForcedFinalCallMiddleware's forced-call invoke_config.
    invoke_config = {"recursion_limit": 80}

    # Per-invocation run context shared with middlewares via ContextVar.
    # Middlewares read case_id / langfuse_handler / langfuse_trace_id and
    # initialize ctx_budget + call_history in LangfuseTracingMiddleware.abefore_agent.
    run_ctx = DiagnosisRunContext(
        case_id=state.case_id or "",
        langfuse_handler=langfuse_handler,
        langfuse_trace_id=state.langfuse_trace_id,
        system_prompt_text=base_prompt,
        evidence_text=evidence_text,
    )
    set_run_context(run_ctx)

    try:
        agent = get_diagnosis_agent()
        # invoke_config = {"recursion_limit": 80} (set above, no callbacks — see
        # note on LangfuseTracingMiddleware.awrap_model_call for why).
        # recursion_limit is a hard backstop ONLY — BudgetGuardMiddleware's
        # model_call_count > MAX_TOOL_CALLS(12) is the primary LLM-call cap
        # (matches range(12) semantics: 12 LLM calls allowed, 13th jumps to end).
        # create_agent implements each middleware hook (before_model / after_model)
        # as a separate graph step, so one ReAct iteration ≈ 4 graph steps
        # (before_model + model + after_model + tools). 12 iterations ≈ 48 steps
        # + the 13th before_model jump ≈ 52; 80 leaves margin so model_call_count
        # is always the binding constraint, and recursion_limit only fires if the
        # middleware counter itself fails.
        result = await agent.ainvoke(
            {"messages": initial_messages},
            config=invoke_config,
        )
        final_messages: list[BaseMessage] = result.get("messages", [])
    except Exception as exc:
        logger.error("diagnosis_agent_exception", error=str(exc), case_id=state.case_id)
        if langfuse_handler is not None:
            with contextlib.suppress(Exception):
                langfuse_handler.end_trace(output_data={"error": str(exc)})
        clear_run_context()
        return handle_agent_failure(state, exc)
    finally:
        # Always clear the ContextVar so a leaked context can't poison the
        # next invocation (middleware instances are reused across invocations).
        clear_run_context()

    budget_exhausted = run_ctx.budget_exhausted
    forced_call_triggered = run_ctx.forced_call_triggered

    report, findings, budget_state, early_stopped = _finalize_report(
        state=state, messages=final_messages, budget_exhausted=budget_exhausted
    )

    _finalize_langfuse_trace(
        langfuse_handler=langfuse_handler,
        report=report,
        early_stopped=early_stopped,
        budget_state=budget_state,
        forced_call_triggered=forced_call_triggered,
        case_id=state.case_id or "",
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
