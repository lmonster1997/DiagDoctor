"""
CopilotKit diagnosis graph — BugInfo → DiagnosisAgent.

This graph replaces the original single-subgraph approach for CopilotKit.
Instead of wrapping only ``get_diagnosis_agent()``, it provides a 2-node
pipeline:

    START → bug_info → diagnosis_agent → END

**BugInfo node**: parses the user's free-text chat message, extracts
structured bug info (description, trigger_time, trace_ids), auto-prefetches
logs+traces from Loki/Tempo, and normalizes them into ``NormalizedEvidence``.

**DiagnosisAgent node**: consumes the normalized evidence identically to
the REST API path — formats it into a system+human message pair, invokes
the ``create_agent`` subgraph (with all 5 middlewares), and produces a
structured diagnosis report.

State schema: typed ``DoctorState`` (TypedDict) so the declared ``add`` reducers
on findings/hypotheses/budget_ticks/total_cost actually run (``messages`` is
overwrite - preserves CopilotKit streaming; add_messages deferred to #5 HITL).
Compiled with a persistent SQLite checkpointer (``_LazyAsyncSqliteSaver`` ->
``data/checkpoints.db``) instead of in-memory MemorySaver. See state.py +
checkpointer.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.engine.agent import (
    _build_system_prompt,
    get_diagnosis_agent,
)
from src.engine.nodes.bug_info import bug_info_node
from src.engine.run_context import (
    DiagnosisRunContext,
    clear_run_context,
    set_run_context,
)
from src.observability.logger import get_logger

if TYPE_CHECKING:
    from src.engine.state import DoctorState

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

    from src.engine.state import NormalizedEvidence
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

    logger.info(
        "copilotkit_diagnosis_agent_invoking",
        case_id=case_id,
        signal_count=len(evidence.golden_signals),
        correlation_count=len(evidence.correlations),
    )

    base_prompt = _build_system_prompt()
    initial_messages: list[BaseMessage] = [
        SystemMessage(content=base_prompt),
        HumanMessage(content=evidence_text),
    ]

    # ── Langfuse setup (graceful degradation) ────────────────────
    langfuse_handler = None
    try:
        langfuse_handler = _get_langfuse_handler_for_dict_state(case_id, evidence_text)
        if langfuse_handler is not None:
            langfuse_handler.prepare_for_managed_trace()
    except Exception:
        pass

    _lf_trace_id = state.get("langfuse_trace_id")
    _lf_session_id = state.get("langfuse_session_id")
    logger.info(
        "copilotkit_diag_langfuse_trace_id",
        case_id=case_id,
        langfuse_trace_id=_lf_trace_id,
        langfuse_session_id=_lf_session_id,
        has_handler=langfuse_handler is not None,
    )

    # Attach Langfuse handler at agent.ainvoke level (NOT model.with_config).
    # Tool callbacks (on_tool_start/end) are no-ops, so no double-recording.
    invoke_config: dict[str, Any] = {"recursion_limit": 80}
    if langfuse_handler is not None:
        invoke_config["callbacks"] = [langfuse_handler]

    run_ctx = DiagnosisRunContext(
        case_id=case_id or "",
        langfuse_handler=langfuse_handler,
        langfuse_trace_id=_lf_trace_id,
        langfuse_session_id=_lf_session_id,
        system_prompt_text=base_prompt,
        evidence_text=evidence_text,
    )
    set_run_context(run_ctx)

    try:
        agent = get_diagnosis_agent()
        result = await agent.ainvoke(  # type: ignore[call-overload]
            {"messages": initial_messages},
            config=invoke_config,
        )
        final_messages: list[BaseMessage] = result.get("messages", [])
    except Exception as exc:
        logger.error("copilotkit_diag_exception", error=str(exc), case_id=case_id)
        if langfuse_handler is not None:
            with contextlib.suppress(Exception):
                langfuse_handler.end_trace(output_data={"error": str(exc)})
        clear_run_context()
        return {"report": None, "findings": []}
    finally:
        clear_run_context()

    budget_exhausted = run_ctx.budget_exhausted
    forced_call_triggered = run_ctx.forced_call_triggered

    # Build a minimal budget_state for _finalize_report
    from src.engine.state import BudgetState

    budget_state = BudgetState()

    report, findings, budget_state, early_stopped = _finalize_report_for_dict_state(
        final_messages, budget_exhausted
    )

    _finalize_langfuse_trace(
        langfuse_handler=langfuse_handler,
        report=report,
        early_stopped=early_stopped,
        budget_state=budget_state,
        forced_call_triggered=forced_call_triggered,
        case_id=case_id or "",
    )

    return {
        "messages": _filter_visible_messages(final_messages),
        "report": report,
        "findings": findings,
        "budget": budget_state,
        "early_stopped": early_stopped,
    }


def _get_langfuse_handler_for_dict_state(case_id: str, evidence_text: str) -> Any | None:
    """Get Langfuse handler (mirrors _get_langfuse_handler for dict state)."""
    try:
        from src.observability.langfuse_tracing import get_langfuse_handler

        return get_langfuse_handler()
    except (ValueError, ImportError):
        return None


def _finalize_report_for_dict_state(
    messages: list[Any], budget_exhausted: bool
) -> tuple[Any, list[Any], Any, bool]:
    """Parse messages into report + findings (mirrors _finalize_report from node.py)."""
    from src.engine.budget.tracker import is_budget_exceeded, update_budget
    from src.engine.parsing import (
        extract_findings,
        parse_diagnosis_report,
    )
    from src.engine.state import BudgetState, DiagnosisReport

    agent_result: dict[str, Any] = {"messages": messages}
    report = parse_diagnosis_report(agent_result)
    findings = extract_findings(agent_result)
    budget_state = update_budget(BudgetState(), agent_result)
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
                "forced_final_json_call": forced_call_triggered,
            },
        )
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════
# Graph construction
# ═════════════════════════════════════════════════════════════════════

_copilotkit_graph_instance: Any = None


def build_copilotkit_graph() -> Any:
    """Build the CopilotKit diagnosis graph.

    2-node linear pipeline:
        START → bug_info → diagnosis_agent → END

    State: typed ``DoctorState`` (TypedDict) so the declared ``add`` reducers
    on ``findings`` / ``hypotheses`` / ``budget_ticks`` / ``total_cost`` actually
    run (the old ``StateGraph(dict)`` declared them but they were dead — node
    returns did dict-overwrite). See ``src/engine/state.py``.

    Compiled with a persistent SQLite checkpointer (``_LazyAsyncSqliteSaver`` ->
    ``data/checkpoints.db``) instead of the in-memory ``MemorySaver``: checkpoints
    now survive process restarts, which is the foundation for #5 HITL
    (``interrupt()`` + resume). The lazy proxy is required because the graph is
    compiled at module-load (sync, no event loop) while ``AsyncSqliteSaver``
    needs a running loop at construction. See ``src/engine/checkpointer.py``.
    """
    from langgraph.graph import END, StateGraph

    from src.engine.checkpointer import make_checkpointer
    from src.engine.state import DoctorState

    builder = StateGraph(DoctorState)

    builder.add_node("bug_info", bug_info_node)
    builder.add_node("diagnosis_agent", _diagnosis_agent_node)

    builder.set_entry_point("bug_info")
    builder.add_edge("bug_info", "diagnosis_agent")
    builder.add_edge("diagnosis_agent", END)

    return builder.compile(checkpointer=make_checkpointer())


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
