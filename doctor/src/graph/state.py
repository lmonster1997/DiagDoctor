"""
LangGraph State definitions for DiagDoctor (v2 — multi-label + Ingest + Critic).

Defines the shared state schema used across all Agent nodes in the diagnosis pipeline.

Key changes from v2:
- DiagnosisReport: single bug_category → primary_category + categories(list)
- DoctorState: added raw_evidence, triage (multi-label), total_cost, retrieval_trace
- DoctorState (v3): removed iterations, critic_feedback, verdict, draft_report
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
    line: str = Field(default="", exclude=True)
    trace_id: str | None = None
    span_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict, exclude=True)

    def model_post_init(self, __context: Any) -> None:
        """Normalise bug-factory field names into doctor field names."""
        # Map 'line' → 'message'
        if not self.message and self.line:
            object.__setattr__(self, "message", self.line)
        # Extract service_name and level from labels if not set at top level
        if self.labels:
            if not self.service_name:
                svc = self.labels.get("service_name", self.labels.get("service", ""))
                if svc:
                    object.__setattr__(self, "service_name", svc)
            if not self.level or self.level == "INFO":
                lvl = self.labels.get("detected_level", self.labels.get("level", ""))
                if lvl:
                    object.__setattr__(self, "level", lvl)
            # Labels may carry trace_id too
            if not self.trace_id:
                tid = self.labels.get("trace_id", "")
                if tid:
                    object.__setattr__(self, "trace_id", tid)


class TraceSpan(BaseModel):
    """A single trace span from Tempo."""

    trace_id: str = ""
    span_id: str
    parent_span_id: str = ""
    name: str = Field(default="", validation_alias="name")
    operation_name: str = Field(default="", exclude=True)
    service: str = ""
    service_name: str = ""
    start: datetime | str = Field(default="", validation_alias="start")
    start_time: datetime | str = Field(default="", exclude=True)
    duration_ms: float = 0.0
    attributes: dict[str, Any] = Field(default_factory=dict)
    status: Literal["ok", "error", "unset"] = "unset"
    db_statement: str = ""

    def model_post_init(self, __context: Any) -> None:
        """Normalise bug-factory field names into doctor field names."""
        if not self.name and self.operation_name:
            object.__setattr__(self, "name", self.operation_name)
        if not self.start and self.start_time:
            object.__setattr__(self, "start", self.start_time)


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
    trigger_time: str | None = Field(
        default=None,
        description="ISO 8601 UTC timestamp of when the bug was triggered. "
        "Used to narrow search_observability queries to a focused time window.",
    )


# ── Ingest / Normalized evidence sub-models ─────────────────────────


class Signal(BaseModel):
    """A golden signal extracted from evidence — the key clues.

    The ``signal_type`` field classifies signals into two families:
    - **Error signals**: crashes, exceptions, 5xx, slow queries — easy
      to detect from logs/traces.
    - **Behavioural mismatch signals**: logic/data/config bugs that
      produce normal HTTP responses but violate expected behaviour
      (IDOR, silent data loss, wrong sort order, etc.). These are
      inferred from the user_report combined with code analysis and
      active API probing — there are no error signals in logs/traces.
    """

    signal_id: str = ""  # e.g. "sig-be001-slow-sql"
    source: Literal["log", "trace", "browser_error", "api_response", "user_report"] = "log"
    signal_type: Literal[
        "error_log",
        "error_span",
        "slow_span",
        "repeated_query",
        "behavior_mismatch",
        "data_invariant_broken",
        "access_control_anomaly",
        "silent_data_loss",
    ] = "error_log"
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
    trigger_time: str | None = Field(
        default=None,
        description="ISO 8601 UTC timestamp of bug trigger, used to narrow "
        "search_observability time window to trigger_time ± 5min.",
    )
    frontend_span_count: int = 0
    backend_span_count: int = 0
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Extra metadata attached by ingest (e.g. frontend_error_spans).",
    )


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
    Shared state for the DiagDoctor LangGraph (v3).

    V3 key changes from v2:
    - Removed: iterations, critic_feedback, verdict (no Critic loop in V3)
    - Removed: draft_report (no synthesis node in V3)
    - Kept: triage field (default-empty, for backward compat; classification
      now embedded in unified_agent System Prompt)
    - Kept: raw_evidence, evidence, findings, hypotheses, report, budget,
      retrieval_trace, total_cost, messages
    """

    # ── Input ──
    raw_evidence: Evidence = Field(default_factory=Evidence)
    case_id: str | None = None

    # ── Ingest layer output ──
    evidence: NormalizedEvidence = Field(default_factory=NormalizedEvidence)

    # ── Triage (multi-label) — default-empty in V3; classification from Agent output ──
    triage: TriageOutput = Field(default_factory=TriageOutput)

    # ── Accumulated findings & hypotheses ──
    findings: Annotated[list[Finding], add] = Field(default_factory=list)
    hypotheses: Annotated[list[DiagnosisHypothesis], add] = Field(default_factory=list)

    # ── Reports (V3: unified_agent produces report directly; no draft_report) ──
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
