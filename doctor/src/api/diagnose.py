"""
Diagnose endpoint — runs the full LangGraph diagnosis pipeline.

Accepts Evidence (user_report + optional logs/traces) and returns
a structured DiagnosisReport. Supports streaming via ?stream=true.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.config import settings
from src.graph.main_graph import generate_thread_id, get_graph
from src.graph.state import DiagnosisReport, Evidence

router = APIRouter(prefix="/api", tags=["diagnose"])

# ── Request / Response models ───────────────────────────────────────


class DiagnoseRequest(BaseModel):
    """Request body for the diagnose endpoint."""

    evidence: Evidence = Field(
        default_factory=Evidence,
        description="Evidence collected for diagnosis (user_report required).",
    )
    thread_id: str | None = Field(
        default=None,
        description="Optional thread_id for resuming a previous session.",
    )


class DiagnoseResponse(BaseModel):
    """Standard (non-streaming) response from the diagnose endpoint."""

    thread_id: str
    report: DiagnosisReport | None = None
    bug_category: str | None = None
    findings_count: int = 0


# ── Internal helpers ────────────────────────────────────────────────


def _build_initial_state(request: DiagnoseRequest, thread_id: str) -> dict[str, Any]:
    """Build the initial DoctorState dict for the graph invocation."""
    return {
        "evidence": request.evidence,
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


async def _stream_graph(
    thread_id: str, state: dict[str, Any]
) -> AsyncIterator[str]:
    """Stream graph events as SSE (Server-Sent Events)."""
    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id}}

    try:
        async for event in graph.astream_events(state, config, version="v2"):
            event_type = event.get("event", "")
            event_name = event.get("name", "")

            # Only stream relevant events to avoid noise
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

        # Signal completion
        yield "data: [DONE]\n\n"

    except Exception:
        yield f"data: {json.dumps({'event': 'error', 'error': 'Graph execution failed'})}\n\n"


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
    bug_category = final_state.get("bug_category")
    findings = final_state.get("findings", [])

    return DiagnoseResponse(
        thread_id=thread_id,
        report=report,
        bug_category=bug_category,
        findings_count=len(findings),
    )

