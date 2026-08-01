"""Per-invocation run context carried via langgraph's ``runtime.context``.

``DiagnosisRunContext`` is passed as ``create_agent(context_schema=...)`` and
injected per-invocation by ``diagnosis_agent_node`` via
``agent.ainvoke(..., context=run_ctx)``. Middlewares read it off the
``runtime`` argument of each hook (``runtime.context``) or off
``request.runtime.context`` in ``awrap_tool_call``.

Why ``runtime.context`` and not ``self`` on the middleware: middleware
instances are reused across ``agent.ainvoke`` calls (langchain binds instance
methods at graph compile time), so any per-invocation mutable state on ``self``
would leak between diagnoses. ``Runtime.context`` is run-scoped -- a fresh
object per ``ainvoke`` -- so it is the correct per-invocation carrier. The
node holds the same object reference it passes in, so mutations made by
middlewares (``ctx_budget`` token accounting, ``call_history`` dedup cache,
``budget_exhausted``, ``forced_call_triggered``) are readable by the node
after ``ainvoke`` returns.

The ``ctx_budget`` is initialised by ``AgentLifecycleMiddleware.abefore_agent``
rather than by the node, so the node only supplies the DoctorState-derived
fields and the system prompt / evidence texts for initial token accounting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.engine.context.budget import ContextBudget


@dataclass
class DiagnosisRunContext:
    """Per-invocation state shared across middlewares via ``runtime.context``.

    Supplied by ``diagnosis_agent_node`` (the values that come from
    ``DoctorState`` and can't be known inside middleware: ``case_id``,
    ``langfuse_handler``) and mutated by middlewares
    during the loop (``ctx_budget`` token accounting, ``call_history`` dedup
    cache, ``elided_tool_call_ids`` elision↔dedup contract, ``model_call_count``,
    ``budget_exhausted``, ``forced_call_triggered``).

    The ``ctx_budget`` is initialised by ``AgentLifecycleMiddleware.abefore_agent``
    rather than by the node, so the node only needs to supply the
    DoctorState-derived fields and the system prompt / evidence texts
    for initial token accounting.
    """

    # ── Supplied by node (from DoctorState - not knowable inside middleware) ──
    case_id: str = ""
    langfuse_handler: Any | None = None

    # ── Initial tokens (node computes these from the system prompt + evidence) ──
    system_prompt_text: str = ""
    evidence_text: str = ""

    # ── Mutated by middlewares during the loop ──
    ctx_budget: ContextBudget = field(default_factory=ContextBudget)
    # call_key (tool_name, args_json) -> tool_call_id of the LAST execution's
    # result. Dict (not list) so dedup can look up which result a prior call
    # produced, and update it when a re-fetch supersedes the old result.
    call_history: dict[tuple[str, str], str] = field(default_factory=dict)
    # tool_call_ids whose ToolMessage has been aged out to a placeholder by
    # ``ContextElisionMiddleware``. Cross-middleware contract: **elision writes
    # (abefore_model), ToolDedupMiddleware reads (awrap_tool_call)** to allow a
    # re-fetch of an elided result instead of skipping it as a wasteful dup
    # (the §7.1 ↔ dedup conflict that caused the recursion-limit loop).
    elided_tool_call_ids: set[str] = field(default_factory=set)
    model_call_count: int = 0
    budget_exhausted: bool = False
    forced_call_triggered: bool = False
