"""
Diagnose endpoint — runs the full LangGraph diagnosis pipeline.

Accepts Evidence (user_report + optional logs/traces/browser_errors)
and returns a structured DiagnosisReport (v2 multi-label).
Supports streaming via ?stream=true.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.engine.nodes.diagnosis_agent import generate_thread_id, get_copilotkit_graph
from src.engine.state import (
    BudgetState,
    Correlation,
    DiagnosisReport,
    Evidence,
    Finding,
    NormalizedEvidence,
)
from src.observability.logger import get_logger

router = APIRouter(prefix="/api", tags=["diagnose"])
logger = get_logger(__name__)

# ── Request / Response models ───────────────────────────────────────


class DiagnoseRequest(BaseModel):
    """Request body for the diagnose endpoint."""

    evidence: Evidence = Field(
        default_factory=Evidence,
        description=(
            "Evidence collected for diagnosis (user_report + optional logs/traces/browser_errors)."
        ),
    )
    thread_id: str | None = Field(
        default=None,
        description="Optional thread_id for resuming a previous session.",
    )
    trigger_time: str | None = Field(
        default=None,
        description=(
            "ISO 8601 UTC timestamp of when the bug was triggered. "
            "Doctor uses this to narrow Loki/Tempo queries to trigger_time ± 5min, "
            "avoiding noisy historical data."
        ),
    )
    trigger_trace_ids: list[str] = Field(
        default_factory=list,
        description=(
            "W3C trace_ids associated with this bug trigger (injected via "
            "`traceparent` on api calls + frontend-captured UI trace_ids). "
            "When present, Doctor ingest prefetches by these trace_ids for "
            "per-case isolation instead of a broad time window — critical in "
            "batch runs where multiple cases fire in the same stack/time window."
        ),
    )
    langfuse_trace_id: str | None = Field(
        default=None,
        description=(
            "Optional Langfuse trace ID to reuse. When provided by the Experiment "
            "runner, the Doctor agent records its LLM/tool observations onto this "
            "trace so that process-quality scorers see the full invocation process "
            "on the same trace that is being scored. None → agent creates a new trace."
        ),
    )
    langfuse_session_id: str | None = Field(
        default=None,
        description=(
            "Optional Langfuse session ID. When provided alongside langfuse_trace_id, "
            "the Doctor agent attaches ALL observations (and any auto-created traces) "
            "to this session, ensuring they appear in the same Langfuse Sessions view "
            "as the experiment runner's traces. Critical for batch experiment runs."
        ),
    )


class DiagnoseResponse(BaseModel):
    """Standard (non-streaming) response from the diagnose endpoint (v2).

    Phase 2: carries the full evidence chain payload (budget, findings,
    normalized evidence, correlations) so the frontend can render
    the EvidenceChainGraph and a complete ReportPanel without a second
    round-trip.
    """

    thread_id: str
    report: DiagnosisReport | None = None
    primary_category: str | None = None
    categories: list[str] = Field(default_factory=list)
    findings_count: int = 0

    # ── Phase 2: evidence chain payload ──
    budget: BudgetState | None = None
    findings: list[Finding] = Field(default_factory=list)
    evidence: NormalizedEvidence | None = None
    correlations: list[Correlation] = Field(default_factory=list)


# ── Internal helpers ────────────────────────────────────────────────


def _build_initial_state(request: DiagnoseRequest) -> dict[str, Any]:
    """Build the initial DoctorState dict for the graph invocation (v2).

    case_id/trace_id/session_id are NOT set here -- the graph's entry node
    (``bug_info_node``) owns them, deriving from ``config.thread_id`` so
    case_id == checkpoint thread_id by construction (single source of truth,
    works for both REST and CopilotKit paths). See ``bug_info.py``.
    """
    # Inject trigger_time into evidence if provided at top level
    if request.trigger_time and not request.evidence.trigger_time:
        request.evidence.trigger_time = request.trigger_time
    # Inject trigger_trace_ids into evidence if provided at top level
    if request.trigger_trace_ids and not request.evidence.trigger_trace_ids:
        request.evidence.trigger_trace_ids = request.trigger_trace_ids

    return {
        "raw_evidence": request.evidence,
        "langfuse_trace_id": request.langfuse_trace_id,
        "langfuse_session_id": request.langfuse_session_id,
    }


async def _run_graph(thread_id: str, state: dict[str, Any]) -> Any:
    """Run the unified bug_info → diagnosis_agent graph."""
    graph = get_copilotkit_graph()
    config = {"configurable": {"thread_id": thread_id}}
    result: Any = await graph.ainvoke(state, config)
    return result


def _extract_evidence_payload(final_state: Any) -> dict[str, Any]:
    """Pull the Phase 2 evidence-chain fields out of the final graph state.

    Returns a dict with keys: budget, findings, evidence, correlations,
    timeline — each serialisable by Pydantic.  ``correlations`` and
    ``timeline`` are surfaced from inside ``NormalizedEvidence`` for the
    frontend's convenience (the graph stores them nested there).
    """
    budget = final_state.get("budget")
    findings = final_state.get("findings", []) or []
    evidence = final_state.get("evidence")

    correlations: list[Correlation] = []
    if evidence is not None and hasattr(evidence, "correlations"):
        correlations = list(evidence.correlations or [])

    return {
        "budget": budget,
        "findings": list(findings),
        "evidence": evidence,
        "correlations": correlations,
    }


async def _stream_graph(thread_id: str, state: dict[str, Any]) -> AsyncIterator[str]:
    """Stream graph events as SSE (Server-Sent Events).

    Emits incremental ``on_chat_model_*`` events during the run, then a
    final ``final`` event carrying the full Phase 2 payload (report +
    budget + findings + evidence + correlations) so the
    frontend can render the evidence chain graph and complete report
    without a second request.
    """
    graph = get_copilotkit_graph()
    config = {"configurable": {"thread_id": thread_id}}

    try:
        async for event in graph.astream_events(state, config, version="v2"):
            event_type = event.get("event", "")
            event_name = event.get("name", "")

            # Stream chat model events
            if event_type in ("on_chat_model_start", "on_chat_model_stream", "on_chat_model_end"):
                data = {
                    "event": event_type,
                    "name": event_name,
                    "data": event.get("data", {}),
                }
                yield f"data: {json.dumps(data, default=str)}\n\n"

            elif event_type == "on_chain_end" and event_name == "reporter":
                # Extract final report from the chain output
                output = event.get("data", {}).get("output", {})
                if isinstance(output, dict) and "report" in output:
                    report = output["report"]
                    if hasattr(report, "model_dump"):
                        report = report.model_dump()
                    data = {
                        "event": "report",
                        "report": report,
                    }
                    yield f"data: {json.dumps(data, default=str)}\n\n"

        # ── Final event: full evidence-chain payload ──
        # Fetch the persisted checkpoint state to access budget/findings/
        # evidence/correlations/timeline alongside the report.
        snapshot = await graph.aget_state(config)

        # -- #5 HITL: if the graph paused (budget exhausted before
        # convergence), surface the interrupt so the client can collect
        # guidance and POST /api/diagnose/resume instead of treating the
        # best-effort early_stopped report as final. --
        if snapshot.next:
            interrupt_payload: dict[str, Any] | None = None
            for task in snapshot.tasks:
                if task.interrupts:
                    interrupt_payload = task.interrupts[0].value
                    break
            hitl_data = {
                "event": "hitl_interrupt",
                "thread_id": thread_id,
                "prompt": (interrupt_payload or {}).get("prompt", "预算耗尽,请补充人工引导。"),
                "prior_findings_count": (interrupt_payload or {}).get("prior_findings_count", 0),
                "early_stopped": bool((snapshot.values or {}).get("early_stopped")),
                "next": list(snapshot.next),
            }
            yield f"data: {json.dumps(hitl_data, default=str)}\n\n"
            return

        final_state: dict[str, Any] = snapshot.values or {}
        payload = _extract_evidence_payload(final_state)

        report = final_state.get("report")
        report_dump = report.model_dump() if report and hasattr(report, "model_dump") else report

        budget = payload["budget"]
        budget_dump = budget.model_dump() if budget and hasattr(budget, "model_dump") else budget

        evidence = payload["evidence"]
        evidence_dump = (
            evidence.model_dump() if evidence and hasattr(evidence, "model_dump") else evidence
        )

        findings_dump = [
            f.model_dump() if hasattr(f, "model_dump") else f for f in payload["findings"]
        ]
        correlations_dump = [
            c.model_dump() if hasattr(c, "model_dump") else c for c in payload["correlations"]
        ]

        final_data = {
            "event": "final",
            "report": report_dump,
            "budget": budget_dump,
            "findings": findings_dump,
            "evidence": evidence_dump,
            "correlations": correlations_dump,
        }
        yield f"data: {json.dumps(final_data, default=str)}\n\n"
    except Exception as exc:
        error_data = {"event": "error", "message": str(exc)}
        yield f"data: {json.dumps(error_data, default=str)}\n\n"
    finally:
        yield "data: [DONE]\n\n"


# ── Routes ──────────────────────────────────────────────────────────


@router.post("/diagnose", response_model=None)
async def diagnose(
    request: DiagnoseRequest,
    stream: bool = Query(False, description="If true, stream events as SSE."),
) -> DiagnoseResponse | StreamingResponse:
    """
    Diagnose a bug using the LangGraph pipeline.

    Accepts Evidence (user_report + optional logs/traces) and runs the
    DiagDoctor graph to produce a DiagnosisReport.

    Set ?stream=true to receive Server-Sent Events for real-time progress.
    Provide a thread_id to resume a previous diagnosis session.
    """
    # Validate: user_report is required
    if not request.evidence.user_report.strip():
        raise HTTPException(
            status_code=422,
            detail="evidence.user_report must not be empty.",
        )

    thread_id = request.thread_id or generate_thread_id()
    initial_state = _build_initial_state(request)

    logger.info("diagnose_request_start", thread_id=thread_id, stream=stream)

    # Streaming mode
    if stream:
        return StreamingResponse(
            _stream_graph(thread_id, initial_state),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Thread-ID": thread_id,
            },
        )

    # Standard (batch) mode
    try:
        final_state = await _run_graph(thread_id, initial_state)
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Graph execution failed: {e}",
        ) from e

    return _response_from_state(thread_id, final_state)


# -- #5 HITL: resume a paused diagnosis + list resumable threads ----------


def _response_from_state(thread_id: str, final_state: dict[str, Any]) -> DiagnoseResponse:
    """Build a DiagnoseResponse from a graph state dict (shared by diagnose + resume)."""
    report = final_state.get("report")
    # V3: categories are embedded in DiagnosisReport (diagnosis_agent output),
    # NOT in triage (triage node was removed in V3).
    primary_category: str | None = None
    categories: list[str] = []
    if report is not None:
        if hasattr(report, "primary_category"):
            primary_category = report.primary_category
        if hasattr(report, "categories"):
            categories = list(report.categories) if report.categories else []
    findings = final_state.get("findings", [])
    payload = _extract_evidence_payload(final_state)
    return DiagnoseResponse(
        thread_id=thread_id,
        report=report,
        primary_category=primary_category,
        categories=categories,
        findings_count=len(findings),
        budget=payload["budget"],
        findings=payload["findings"],
        evidence=payload["evidence"],
        correlations=payload["correlations"],
    )


class ResumeRequest(BaseModel):
    """Request body for the diagnose resume endpoint (#5 HITL)."""

    thread_id: str = Field(..., description="The paused diagnosis thread_id to resume.")
    guidance: str = Field(
        default="",
        description=(
            "Operator guidance line. Non-empty re-enters the diagnosis agent "
            "for an informed second pass; empty accepts the current best-effort "
            "(early_stopped) report."
        ),
    )


@router.post("/diagnose/resume", response_model=None)
async def resume_diagnosis(request: ResumeRequest) -> DiagnoseResponse:
    """Resume a paused (HITL-interrupted) diagnosis with operator guidance.

    The diagnosis must have paused at the ``human_input`` node (budget
    exhausted before convergence, ``early_stopped``). Non-empty ``guidance``
    re-enters the diagnosis agent for an informed second pass; empty guidance
    accepts the current best-effort report. Returns the (possibly improved)
    final report after the resumed run.
    """
    from langgraph.types import Command

    graph = get_copilotkit_graph()
    config: dict[str, Any] = {"configurable": {"thread_id": request.thread_id}}

    snapshot = await graph.aget_state(config)
    if not snapshot or not snapshot.next:
        raise HTTPException(
            status_code=409,
            detail=(
                "No paused diagnosis to resume for this thread_id "
                "(none exists or it already completed)."
            ),
        )

    try:
        await graph.ainvoke(Command(resume=request.guidance), config)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Resume failed: {e}") from e

    final_state = (await graph.aget_state(config)).values or {}
    logger.info(
        "diagnose_resume_done",
        thread_id=request.thread_id,
        early_stopped=bool(final_state.get("early_stopped")),
        hitl_resumed=bool(final_state.get("hitl_resumed")),
        guidance_len=len(request.guidance),
    )
    return _response_from_state(request.thread_id, final_state)


@router.get("/diagnose/threads", response_model=None)
async def list_diagnosis_threads(
    limit: int = Query(50, ge=1, le=200, description="Max threads to return."),
) -> dict[str, Any]:
    """List recent diagnosis threads from the checkpoint store.

    Thin enabler for a frontend 'resume a paused diagnosis' list. Each entry
    is the latest checkpoint for a thread:

    - ``paused``: mid-graph (e.g. awaiting HITL guidance) -> resumable via
      POST /api/diagnose/resume.
    - ``completed``: reached END with a report.
    - ``empty``: no values yet.

    Paused threads are listed first.
    """
    graph = get_copilotkit_graph()
    saver = getattr(graph, "checkpointer", None)
    if saver is None:
        return {"threads": []}

    # alist yields checkpoints latest-first across all threads AND subgraphs:
    # the ``diagnosis_agent`` node runs an inner compiled subgraph whose
    # checkpoints share this saver under a "diagnosis_agent" namespace. We only
    # want the outer-graph (root) state per thread, so dedup by thread_id and
    # re-fetch the latest root state via ``aget_state({thread_id})``. Passing a
    # subgraph checkpoint's config directly makes langgraph try to resolve the
    # "diagnosis_agent" subgraph (a function node, not registered) -> ValueError
    # "Subgraph diagnosis_agent not found".
    seen: set[str] = set()
    thread_ids: list[str] = []
    async for tup in saver.alist(None, limit=limit * 10):
        tid = (tup.config or {}).get("configurable", {}).get("thread_id")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        thread_ids.append(tid)
        if len(thread_ids) >= limit:
            break

    threads: list[dict[str, Any]] = []
    for tid in thread_ids:
        try:
            snap = await graph.aget_state({"configurable": {"thread_id": tid}})
        except ValueError:
            # Stale / subgraph-only checkpoint we can't resolve at the root.
            continue
        vals = snap.values or {}
        is_paused = bool(snap.next)
        report = vals.get("report")
        status = "paused" if is_paused else ("completed" if report else "empty")
        threads.append(
            {
                "thread_id": tid,
                "case_id": vals.get("case_id"),
                "status": status,
                "early_stopped": bool(vals.get("early_stopped")),
                "hitl_resumed": bool(vals.get("hitl_resumed")),
                "findings_count": len(vals.get("findings") or []),
                "has_report": bool(report),
                "next": list(snap.next or []),
            }
        )
    threads.sort(key=lambda t: (t["status"] != "paused",))
    return {"threads": threads}
