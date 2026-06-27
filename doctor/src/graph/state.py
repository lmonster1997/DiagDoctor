"""
LangGraph State definitions for DiagDoctor (v2 — multi-label + Ingest + Critic).

Defines the shared state schema used across all Agent nodes in the diagnosis pipeline.

Key changes from v1:
- DiagnosisReport: single bug_category → primary_category + categories(list)
- DoctorState: added raw_evidence, triage (multi-label), iterations, critic_feedback,
  verdict, draft_report, total_cost, retrieval_trace
- New sub-models: NormalizedEvidence, Signal, TimelineEvent, Correlation, TriageOutput,
  CategoryScore, RetrievalRecord, BrowserError
"""

from datetime import datetime
from operator import add
from typing import Annotated, Any, Literal

from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

# ── Evidence sub-models ──────────────────────────────────────────────


class LogEntry(BaseModel):
    """A single log entry from Loki."""

    timestamp: datetime | str = ""
    level: str = "INFO"
    service: str = ""
    service_name: str = ""
    message: str = ""
    trace_id: str | None = None
    span_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class TraceSpan(BaseModel):
    """A single trace span from Tempo."""

    span_id: str
    parent_span_id: str = ""
    name: str = ""
    service: str = ""
    service_name: str = ""
    start: datetime | str = ""
    duration_ms: float = 0.0
    attributes: dict[str, Any] = Field(default_factory=dict)
    status: Literal["ok", "error", "unset"] = "unset"
    db_statement: str = ""


class BrowserError(BaseModel):
    """A browser-side error captured by Playwright/OTel-JS."""

    message: str = ""
    source: str = ""
    lineno: int = 0
    colno: int = 0
    stack: str = ""
    trace_id: str | None = None
    span_id: str | None = None
    component_stack: str = ""
    breadcrumbs: list[dict[str, Any]] = Field(default_factory=list)
    timestamp: str = ""


class Evidence(BaseModel):
    """Raw evidence collected for diagnosis (user-facing input)."""

    user_report: str = ""
    logs: list[LogEntry] = Field(default_factory=list)
    traces: list[TraceSpan] = Field(default_factory=list)
    browser_errors: list[BrowserError] = Field(default_factory=list)
    error_screenshot_url: str | None = None
    request: dict[str, Any] | None = None
    response: dict[str, Any] | None = None


# ── Ingest / Normalized evidence sub-models ─────────────────────────


class Signal(BaseModel):
    """A golden signal extracted from evidence — the key clues."""

    signal_id: str = ""  # e.g. "sig-be001-slow-sql"
    source: Literal["log", "trace", "browser_error", "api_response"] = "log"
    service_tier: Literal["frontend", "backend"] = "backend"
    severity: Literal["error", "warning", "info"] = "error"
    summary: str = ""
    evidence_ref: str = ""  # reference ID to the raw evidence
    timestamp: datetime | str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class TimelineEvent(BaseModel):
    """A single event in the merged cross-source timeline."""

    timestamp: datetime | str = ""
    source: Literal["log", "trace", "browser_error", "api"] = "log"
    service_tier: Literal["frontend", "backend"] = "backend"
    service_name: str = ""
    description: str = ""
    evidence_ref: str = ""
    trace_id: str | None = None


class Correlation(BaseModel):
    """Cross-layer correlation: links evidence across frontend/backend/DB."""

    correlation_id: str = ""
    trace_id: str | None = None
    description: str = ""
    frontend_signals: list[str] = Field(default_factory=list)  # signal_ids
    backend_signals: list[str] = Field(default_factory=list)
    db_signals: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class NormalizedEvidence(BaseModel):
    """Normalized evidence after the Ingest layer processing."""

    user_report: str = ""
    golden_signals: list[Signal] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    correlations: list[Correlation] = Field(default_factory=list)
    raw_refs: dict[str, Any] = Field(default_factory=dict)  # index to raw evidence
    noise_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    frontend_span_count: int = 0
    backend_span_count: int = 0


# ── Triage sub-models (multi-label) ──────────────────────────────────

BugCategory = Literal["frontend_crash", "backend_error", "performance", "logic", "data", "config"]

VALID_CATEGORIES: frozenset[str] = frozenset(
    {"frontend_crash", "backend_error", "performance", "logic", "data", "config"}
)


class CategoryScore(BaseModel):
    """Confidence score for a single bug category."""

    category: str = ""  # BugCategory
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class TriageOutput(BaseModel):
    """Multi-label triage output with confidence distribution.

    Replaces the v1 single-category Literal output.
    """

    scores: list[CategoryScore] = Field(default_factory=list)
    primary: str = ""
    reasoning: str = ""
    cross_layer_suspected: bool = False


# ── Analysis sub-models ─────────────────────────────────────────────


class Finding(BaseModel):
    """A finding from an individual Specialist Agent."""

    agent: str = ""
    summary: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    fix_suggestion: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    cross_layer: bool = False  # true if this finding points to a different tier root cause
    contradiction: bool = False  # true if evidence contradicts the initial classification


class DiagnosisHypothesis(BaseModel):
    """A hypothesis about the root cause — must ground to evidence."""

    summary: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    affected_files: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    proposed_by: str = ""


class RetrievalRecord(BaseModel):
    """A record of one retrieval call for evaluation/tracing."""

    query: str = ""
    source: Literal["code_search", "triage_rag", "knowledge_rag", "case_store"] = "code_search"
    retrieved: list[dict[str, Any]] = Field(default_factory=list)  # [{chunk_id, score}, ...]
    k: int = 5
    timestamp: datetime | str = ""


class DiagnosisReport(BaseModel):
    """Final diagnosis report (v2 — multi-label fields).

    ⚠️  v2 changes:
        - bug_category (single str) → primary_category + categories(list)
        - Added: symptom_tier, root_cause_tier, early_stopped, notes
    """

    primary_category: str = ""  # highest-confidence category (replaces old bug_category)
    categories: list[str] = Field(default_factory=list)  # full multi-label set
    symptom_tier: Literal["frontend", "backend"] = "backend"
    root_cause_tier: Literal["frontend", "backend", "data"] = "backend"
    root_cause: str = ""
    affected_file: str | None = None
    affected_line: int | None = None
    fix_suggestion: str = ""
    evidence_chain: list[str] = Field(default_factory=list)  # evidence_refs chain
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    early_stopped: bool = False  # budget gate forced early exit
    notes: str = ""


# ── Budget guard ────────────────────────────────────────────────────


class BudgetState(BaseModel):
    """Tracks per-diagnosis resource usage for the budget gate."""

    total_tokens: int = 0
    total_cost_usd: float = 0.0
    tool_calls: int = 0
    started_at: datetime | None = None
    elapsed_seconds: float = 0.0
    last_checked_at: datetime | None = None


# ── Main State ──────────────────────────────────────────────────────


class DoctorState(BaseModel):
    """
    Shared state for the DiagDoctor LangGraph (v2).

    Key changes from v1:
    - evidence is now raw_evidence (original input)
    - normalized evidence is stored in evidence (NormalizedEvidence)
    - triage is multi-label TriageOutput (not single Literal)
    - Added: iterations, critic_feedback, verdict, draft_report, total_cost, retrieval_trace
    - Removed: bug_category (migrated to triage.primary)
    """

    # ── Input ──
    raw_evidence: Evidence = Field(default_factory=Evidence)
    case_id: str | None = None

    # ── Ingest layer output ──
    evidence: NormalizedEvidence = Field(default_factory=NormalizedEvidence)

    # ── Triage (multi-label) ──
    triage: TriageOutput = Field(default_factory=TriageOutput)

    # ── Accumulated findings & hypotheses ──
    findings: Annotated[list[Finding], add] = Field(default_factory=list)
    hypotheses: Annotated[list[DiagnosisHypothesis], add] = Field(default_factory=list)

    # ── Critic loop control ──
    iterations: int = 0
    critic_feedback: str | None = None
    verdict: Literal["accept", "retry"] | None = None

    # ── Reports ──
    draft_report: DiagnosisReport | None = None
    report: DiagnosisReport | None = None

    # ── Message history (for ReAct agents) ──
    messages: Annotated[list[Any], add_messages] = Field(default_factory=list)

    # ── Cost & budget ──
    total_cost: Annotated[float, add] = 0.0
    budget: BudgetState = Field(default_factory=BudgetState)

    # ── Retrieval trace (for RAG evaluation) ──
    retrieval_trace: Annotated[list[RetrievalRecord], add] = Field(default_factory=list)

    # ── Metadata ──
    trace_id: str = ""
    session_id: str = ""
