"""Per-invocation run context shared across middlewares via ContextVar.

Middleware instances are reused across ``agent.ainvoke`` calls (langchain
binds instance methods at graph compile time), so any per-invocation mutable
state on ``self`` would leak between diagnoses. Instead, the
``diagnosis_agent_node`` sets a ``DiagnosisRunContext`` into a module-level
``ContextVar`` right before calling ``agent.ainvoke``, and clears it in a
``finally`` block after. Middlewares read/write the context via
``get_run_context()``.

This mirrors the previous hand-written loop's function-local state
(``call_history``, ``ctx_budget``, ``budget_exhausted``) — just relocated to a
ContextVar so middlewares can reach it without ``self`` mutation.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from src.engine.context.budget import ContextBudget


@dataclass
class DiagnosisRunContext:
    """Per-invocation state shared across middlewares.

    Set by ``diagnosis_agent_node`` (the values that come from ``DoctorState``
    and can't be known inside middleware: ``case_id``, ``langfuse_handler``,
    ``langfuse_trace_id``) and mutated by middlewares during the loop
    (``ctx_budget`` token accounting, ``call_history`` dedup cache,
    ``model_call_count``, ``budget_exhausted``, ``forced_call_triggered``).

    The ``ctx_budget`` is initialized by ``AgentLifecycleMiddleware.abefore_agent``
    rather than by the node, so the node only needs to supply the
    DoctorState-derived fields and the system prompt / evidence texts
    for initial token accounting.
    """

    # ── Supplied by node (from DoctorState — not knowable inside middleware) ──
    case_id: str = ""
    langfuse_handler: Any | None = None
    langfuse_trace_id: str | None = None
    langfuse_session_id: str | None = None

    # ── Initial tokens (node computes these from the system prompt + evidence) ──
    system_prompt_text: str = ""
    evidence_text: str = ""

    # ── Mutated by middlewares during the loop ──
    ctx_budget: ContextBudget = field(default_factory=ContextBudget)
    call_history: list[tuple[str, str]] = field(default_factory=list)
    model_call_count: int = 0
    budget_exhausted: bool = False
    forced_call_triggered: bool = False


# Module-level ContextVar. Default None so a missing set raises clearly rather
# than silently using a leaked context from a prior invocation.
_run_ctx: ContextVar[DiagnosisRunContext | None] = ContextVar("diagnosis_run_ctx", default=None)


def set_run_context(ctx: DiagnosisRunContext) -> None:
    """Set the run context for the current invocation (call before agent.ainvoke)."""
    _run_ctx.set(ctx)


def get_run_context() -> DiagnosisRunContext:
    """Get the current invocation's run context.

    Raises ``LookupError`` if no context is set — middlewares should never run
    outside a node-invoked ``agent.ainvoke``, so this is a programmer error
    rather than a runtime condition to silently handle.
    """
    ctx = _run_ctx.get()
    if ctx is None:
        raise LookupError(
            "DiagnosisRunContext not set — diagnosis_agent_node must call "
            "set_run_context() before agent.ainvoke()"
        )
    return ctx


def get_run_context_or_none() -> DiagnosisRunContext | None:
    """Get the run context, or None if not set (for defensive paths)."""
    return _run_ctx.get()


def clear_run_context() -> None:
    """Clear the run context (call in node's finally block after agent.ainvoke)."""
    _run_ctx.set(None)
