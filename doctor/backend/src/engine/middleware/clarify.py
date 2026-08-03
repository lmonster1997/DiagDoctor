"""ClarificationMiddleware - P1 active clarification: stop the loop when the agent asks.

Registered after BudgetGuard, before ForcedFinalCall. When the agent emits a
``request_user_clarification`` tool_call, the tool executes (returns an ack) and
the loop would otherwise continue to the next model call -- the agent keeps
flailing past its own question. This middleware detects that tool_call on the
*next* ``abefore_model`` (after the tool ran) and ``jump_to="end"`` stops the
inner ReAct loop cleanly. It also stashes the question on the run context so
``_diagnosis_agent_node`` can route to the outer-graph ``clarify_input``
interrupt node with the question in hand.

Why ``abefore_model`` and not the tool itself: langchain tools wrapped via
``StructuredTool.from_function`` don't receive ``runtime.context``, so a tool
can't set the run-context flag directly. The middleware sees the tool_call in
``state.messages`` and has ``runtime.context`` -- single extraction point, no
tool-context injection plumbing. Mirrors ``BudgetGuardMiddleware``'s
``jump_to="end"`` pattern (verified working in this stack).

Timing: fires at the start of iteration N+1, after the clarify tool executed in
iteration N -- so the ack ToolMessage exists (no dangling tool_call) and the
flag is set before ``ForcedFinalCall.aafter_agent`` runs (which guards on
``ctx.clarification_requested`` to skip the forced JSON call -- pausing needs no
final report).
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, BaseMessage

from src.observability.logger import get_logger

logger = get_logger(__name__)

_CLARIFY_TOOL = "request_user_clarification"


def _last_clarify_call(messages: list[BaseMessage]) -> dict[str, Any] | None:
    """Return the args of the most recent ``request_user_clarification`` tool_call.

    Scans messages in reverse for the last AIMessage carrying a clarify tool_call
    and returns that call's args dict (``{"question": ...}``); None if none.
    """
    for msg in reversed(messages):
        if not isinstance(msg, AIMessage):
            continue
        tcs = getattr(msg, "tool_calls", None) or []
        for tc in tcs:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
            if name == _CLARIFY_TOOL:
                return tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
        # Only inspect the most recent AIMessage -- a clarify call followed by a
        # later converged AIMessage (no clarify) means the agent moved on, so we
        # don't pause. Break after the first AIMessage we see.
        break
    return None


class ClarificationMiddleware(AgentMiddleware):
    """Stop the ReAct loop when the agent proactively asks the user a question.

    On each ``abefore_model``, if the agent's last AIMessage emitted a
    ``request_user_clarification`` tool_call, record the question on the run
    context and ``jump_to="end"`` so the outer node can route to the
    ``clarify_input`` interrupt. Idempotent: if already requested this pass, the
    jump just re-fires harmlessly.
    """

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        ctx = runtime.context
        if ctx is None:
            return None
        if getattr(ctx, "clarification_requested", False):
            # Already requested this pass (e.g. re-entry) -- keep the loop stopped.
            return {"jump_to": "end"}

        messages: list[BaseMessage] = (
            state.get("messages", []) if isinstance(state, dict) else []
        )
        args = _last_clarify_call(messages)
        if args is None:
            return None

        question = ""
        if isinstance(args, dict):
            q = args.get("question")
            if isinstance(q, str):
                question = q
        ctx.clarification_requested = True
        ctx.clarification_question = question
        logger.info(
            "active_clarification_requested",
            case_id=getattr(ctx, "case_id", ""),
            question_len=len(question),
        )
        return {"jump_to": "end"}
