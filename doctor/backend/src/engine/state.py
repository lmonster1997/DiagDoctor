"""
LangGraph State definitions for DiagDoctor.

Defines the shared state schema used across all Agent nodes in the diagnosis pipeline.

Key changes from v2:
- DiagnosisReport: single bug_category → primary_category + categories(list)
- DoctorState: added raw_evidence, triage (multi-label), total_cost
- DoctorState (v3): removed iterations, critic_feedback, verdict, draft_report
- New sub-models: NormalizedEvidence, Signal, Correlation
"""

from datetime import datetime
from operator import add
from typing import Annotated, Any, Literal

from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

# NOTE: TypedDict must come from typing_extensions, not typing -- langgraph's
# StateGraph schema introspection (used by CopilotKit's ag-ui path) rejects
# typing.TypedDict with "Please use typing_extensions.TypedDict".

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


class Evidence(BaseModel):
    """Raw evidence collected for diagnosis (user-facing input)."""

    user_report: str = ""
    logs: list[LogEntry] = Field(default_factory=list)
    traces: list[TraceSpan] = Field(default_factory=list)
    error_screenshot_url: str | None = None
    request: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    trigger_time: str | None = Field(
        default=None,
        description="ISO 8601 UTC timestamp of when the bug was triggered. "
        "Used to narrow search_observability queries to a focused time window.",
    )
    trigger_trace_ids: list[str] = Field(
        default_factory=list,
        description="W3C trace_ids associated with this bug trigger (injected "
        "via `traceparent` on api calls + frontend-captured UI trace_ids). "
        "When present, ingest prefetches by these trace_ids for per-case "
        "isolation instead of a broad time window.",
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
    source: Literal["log", "trace", "user_report"] = "log"
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
    correlations: list[Correlation] = Field(default_factory=list)
    trigger_time: str | None = Field(
        default=None,
        description="ISO 8601 UTC timestamp of bug trigger, used to narrow "
        "search_observability time window to trigger_time ± 5min.",
    )
    trigger_trace_ids: list[str] = Field(
        default_factory=list,
        description="W3C trace_ids associated with THIS trigger. When present, "
        "the agent can query Tempo/Loki precisely by them for per-case isolation.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Extra metadata attached by ingest (e.g. frontend_error_spans).",
    )


# ── Triage sub-models (multi-label) ──────────────────────────────────

BugCategory = Literal["frontend_crash", "backend_error", "performance", "logic", "data", "config"]

VALID_CATEGORIES: frozenset[str] = frozenset(
    {"frontend_crash", "backend_error", "performance", "logic", "data", "config"}
)


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


class DiagnosisReport(BaseModel):
    """Final diagnosis report (v2 — multi-label fields)."""

    primary_category: str = ""
    categories: list[str] = Field(default_factory=list)
    symptom_tier: Literal["frontend", "backend"] = "backend"
    root_cause_tier: Literal["frontend", "backend", "data"] = "backend"
    root_cause: str = ""
    affected_file: str | None = None
    affected_function: str | None = None
    fix_suggestion: str = ""
    evidence_chain: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    early_stopped: bool = False
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


class DoctorState(TypedDict, total=False):
    """Shared state schema for the DiagDoctor LangGraph (v3).

    **TypedDict, not Pydantic** -- this is the *graph* state schema. LangGraph
    passes a plain ``dict`` to nodes when the schema is a TypedDict, so existing
    nodes/middlewares keep using ``state.get(...)`` and their
    ``isinstance(state, dict)`` guards (a Pydantic schema would hand nodes a
    model instance with no ``.get()``, breaking BudgetGuard/ForcedFinalCall).

    Reducers (the fix for the "declared-but-dead reducer" anti-pattern from the
    old ``StateGraph(dict)`` -- where ``Annotated[..., add]`` was declared but
    never ran and node returns did dict-overwrite):

    - ``findings`` / ``hypotheses`` / ``budget_ticks``: ``add`` -> accumulate
      across nodes. Essential for #5 HITL resume (a resumed run appends to
      prior findings instead of clobbering them).
    - ``total_cost``: ``add`` -> accumulate cost across the run.
    - ``messages``: ``add_messages`` -> accumulate visible (AI/Tool) messages
      across nodes and across HITL passes. #5 HITL resume: the chat history
      must persist across the pause/resume boundary so the operator sees
      pass-1 reasoning -> pause -> pass-2 reasoning in one thread. With the
      old ``overwrite`` semantics, the second ``diagnosis_agent`` pass would
      clobber pass-1's visible messages from the synced state. ``add_messages``
      is the LangGraph idiom for an append-only message channel and is what
      CopilotKit's ag-ui state sync expects. Safe for the REST/benchmark path:
      that path carries no input chat messages, so accumulation == overwrite
      there (no regression). See ``docs/followup-plan-20260715.md`` #5/#7.

    All fields are optional (``total=False``): nodes return only the keys they
    write; LangGraph initialises missing channels to ``None`` (non-reducer) or
    via the reducer's zero (``add`` -> empty).

    V3 key changes from v2:
    - Removed: iterations, critic_feedback, verdict (no Critic loop in V3)
    - Removed: draft_report (no synthesis node in V3)
    - Kept: raw_evidence, evidence, findings, hypotheses, report, budget,
      total_cost, messages
    """

    # ── Input ──
    raw_evidence: Evidence
    case_id: str | None

    # ── Ingest layer output ──
    evidence: NormalizedEvidence

    # ── Accumulated findings & hypotheses (add reducer) ──
    findings: Annotated[list[Finding], add]
    hypotheses: Annotated[list[DiagnosisHypothesis], add]

    # ── Reports (V3: diagnosis_agent produces report directly; no draft_report) ──
    report: DiagnosisReport | None

    # ── Message history (add_messages - see class docstring) ──
    messages: Annotated[list[Any], add_messages]

    # ── Cost & budget ──
    total_cost: Annotated[float, add]
    budget: BudgetState

    # ── Budget ticks for real-time frontend sync ──
    # The BudgetGuardMiddleware appends a snapshot of the running budget
    # before each LLM call.  CopilotKit's useCoAgent syncs this list to
    # the frontend BudgetPanel so the user sees live token/cost/tool_call
    # counters (Phase 1).
    budget_ticks: Annotated[list[dict[str, Any]], add]

    # ── Early-stop flag (set by diagnosis_agent when budget exhausted) ──
    early_stopped: bool

    # ── #5 HITL (interrupt + resume) ──────────────────────────────────
    # ``human_guidance``: the operator's one-line steering hint, written by
    # the ``human_input`` node when it resumes from ``interrupt()``. The
    # ``diagnosis_agent`` node reads it to run an informed second pass.
    # ``hitl_resumed``: one-shot gate -- once True, a second budget exhaustion
    # routes straight to END instead of re-pausing (no infinite HITL loop).
    human_guidance: str | None
    hitl_resumed: bool

    # ── RAG: retrieved historical cases (#1 episodic retrieval) ────────
    # ``retrieved_case_ids``: case_ids recalled on pass 1 -- consumed by the
    # feedback loop (#8 / §8.1) to backfill ``effectiveness`` on 👍.
    # ``similar_cases_text``: the formatted §6.5 injection block, cached on
    # pass 1 so the HITL resume pass re-injects WITHOUT re-querying Qdrant
    # (design §6.5: "only first pass" = don't re-QUERY, not don't re-inject).
    retrieved_case_ids: list[str]
    similar_cases_text: str

    # ── Metadata ──
    trace_id: str
    session_id: str

    # ── Langfuse trace 复用 ID（与 OTel trace_id 语义不同）──
    # 由 Experiment 传入，Agent 节点用它把 LLM/tool observation 记录到
    # 与评分同一个 trace 上。None 时 Agent 自动生成新 trace。
    langfuse_trace_id: str | None
    # ── Langfuse session ID（由 Experiment 传入）──
    # 当 Experiment runner 提供时，覆盖 handler 内部的随机 UUID session，
    # 确保所有 trace/observation 归入正确的 Langfuse Sessions 视图。
    langfuse_session_id: str | None
