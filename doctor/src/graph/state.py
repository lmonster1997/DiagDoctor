"""
LangGraph State definitions for DiagDoctor.

Defines the shared state schema used across all Agent nodes in the diagnosis pipeline.
"""

from datetime import datetime
from operator import add
from typing import Annotated, Literal, Optional

from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


# ── Evidence sub-models ──────────────────────────────────────────────


class LogEntry(BaseModel):
    """A single log entry from Loki."""

    timestamp: datetime
    level: str
    service: str
    message: str
    trace_id: Optional[str] = None
    attributes: dict = Field(default_factory=dict)


class TraceSpan(BaseModel):
    """A single trace span from Tempo."""

    span_id: str
    parent_id: Optional[str] = None
    name: str
    service: str
    start: datetime
    duration_ms: float
    attributes: dict = Field(default_factory=dict)
    status: Literal["ok", "error", "unset"] = "unset"


class Evidence(BaseModel):
    """Evidence collected for diagnosis."""

    user_report: str = ""
    logs: list[LogEntry] = Field(default_factory=list)
    traces: list[TraceSpan] = Field(default_factory=list)
    error_screenshot_url: Optional[str] = None
    request: Optional[dict] = None
    response: Optional[dict] = None


# ── Analysis sub-models ─────────────────────────────────────────────


class Finding(BaseModel):
    """A finding from an individual Agent."""

    agent: str
    summary: str
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class Hypothesis(BaseModel):
    """A hypothesis about the root cause."""

    summary: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    affected_files: list[str] = Field(default_factory=list)
    proposed_by: str = ""


class DiagnosisReport(BaseModel):
    """Final diagnosis report."""

    bug_category: str = ""
    root_cause: str = ""
    affected_file: Optional[str] = None
    affected_line: Optional[int] = None
    fix_suggestion: str = ""
    evidence_chain: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


# ── Main State ──────────────────────────────────────────────────────


class DoctorState(BaseModel):
    """
    Shared state for the DiagDoctor LangGraph.

    Uses Pydantic BaseModel for compatibility with LangGraph's checkpoint system.
    """

    # Input
    evidence: Evidence = Field(default_factory=Evidence)
    case_id: Optional[str] = None

    # Triage result
    bug_category: Optional[
        Literal["frontend_crash", "backend_error", "performance", "logic", "data", "config"]
    ] = None

    # Accumulated findings & hypotheses
    findings: Annotated[list[Finding], add] = Field(default_factory=list)
    hypotheses: Annotated[list[Hypothesis], add] = Field(default_factory=list)

    # Final report
    report: Optional[DiagnosisReport] = None

    # Message history (for ReAct agents)
    messages: Annotated[list, add_messages] = Field(default_factory=list)

    # Metadata
    session_id: str = ""
