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

from src.graph.main_graph import generate_thread_id, get_graph
from src.graph.state import DiagnosisReport, Evidence

router = APIRouter(prefix="/api", tags=["diagnose"])

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


class DiagnoseResponse(BaseModel):
    """Standard (non-streaming) response from the diagnose endpoint (v2)."""

    thread_id: str
    report: DiagnosisReport | None = None
    primary_category: str | None = None
    categories: list[str] = Field(default_factory=list)
    findings_count: int = 0


# ── Internal helpers ────────────────────────────────────────────────


def _build_initial_state(request: DiagnoseRequest, thread_id: str) -> dict[str, Any]:
    """Build the initial DoctorState dict for the graph invocation (v2)."""
    return {
        "raw_evidence": request.evidence,
        "case_id": thread_id,
        "trace_id": thread_id,
        "session_id": thread_id,
    }


async def _run_graph(thread_id: str, state: dict[str, Any]) -> Any:
    """Run the graph and return the final state dict."""
    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id}}
    result: Any = await graph.ainvoke(state, config)
    return result


async def _stream_graph(thread_id: str, state: dict[str, Any]) -> AsyncIterator[str]:
    """Stream graph events as SSE (Server-Sent Events)."""
    graph = get_graph()
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
    initial_state = _build_initial_state(request, thread_id)

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

    report = final_state.get("report")
    triage = final_state.get("triage")
    primary_category: str | None = None
    categories: list[str] = []
    if triage is not None:
        if hasattr(triage, "primary"):
            primary_category = triage.primary
        if hasattr(triage, "scores"):
            categories = [s.category for s in triage.scores]
    findings = final_state.get("findings", [])

    return DiagnoseResponse(
        thread_id=thread_id,
        report=report,
        primary_category=primary_category,
        categories=categories,
        findings_count=len(findings),
    )
