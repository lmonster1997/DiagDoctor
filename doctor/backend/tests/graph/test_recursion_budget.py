"""Regression: BudgetGuard must stop the agent loop before recursion_limit fires.

Root cause of the 2026-07-29 ``Recursion limit of 80 reached`` crash: in
langchain ``create_agent`` each middleware ``before_model``/``after_model`` hook
is a separate graph node (= 1 recursion step). With ContextElision (§7.1) +
BudgetGuard one ReAct iteration costs ~5 steps, so ``recursion_limit=80``
(= 16*5) couldn't accommodate the 17th ``before_model`` where BudgetGuard
(``MAX_MODEL_CALLS=16``) fires -> ``GraphRecursionError`` instead of a graceful
stop, and BudgetGuard was structurally unreachable (no ``budget_iteration_cap_hit``
log). §7.1 added the ``ContextElision.before_model`` node (4 -> 5 steps/iter),
which broke the previously-calibrated 80.

These tests pin the relationship with a fake LLM that never stops calling tools,
so the only thing that can end the loop is BudgetGuard (or recursion). Adding a
loop-step middleware without raising ``RECURSION_LIMIT`` breaks the second test.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.errors import GraphRecursionError

from src.engine.budget.constants import MAX_MODEL_CALLS, RECURSION_LIMIT
from src.engine.context.budget import ContextBudget
from src.engine.middleware import BudgetGuardMiddleware, ContextElisionMiddleware
from src.engine.run_context import (
    DiagnosisRunContext,
    clear_run_context,
    set_run_context,
)


class _AlwaysToolLLM(BaseChatModel):
    """Fake chat model: every call emits one ``echo`` tool call (unique id) so
    the agent loops until BudgetGuard (or recursion) stops it."""

    @property
    def _llm_type(self) -> str:
        return "always-tool-test"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN002, ANN003
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        call_id = f"call_{len(messages)}"  # unique per call -> no tool_call_id collisions
        msg = AIMessage(
            content="",
            tool_calls=[
                {"name": "echo", "args": {"text": "x"}, "id": call_id, "type": "tool_call"}
            ],
        )
        return ChatResult(generations=[ChatGeneration(message=msg)])


@tool
def echo(text: str) -> str:
    """Echo back the input (test-only tool)."""
    return "ok"


def _build_loop_agent() -> Any:
    """Minimal agent with the §7.1 loop-step structure: ContextElision.bm +
    BudgetGuard.bm + model + BudgetGuard.am + tools ≈ 5 steps/iter. The other
    middlewares are awrap_tool_call (inside the tools node) or before/after_agent
    (once, not in loop), so they don't add loop steps - this matches the real
    agent's per-iteration step count."""
    return create_agent(
        model=_AlwaysToolLLM(),
        tools=[echo],
        system_prompt="test",
        middleware=[ContextElisionMiddleware(), BudgetGuardMiddleware()],
    )


def _fresh_ctx() -> DiagnosisRunContext:
    """Run context with an initialised budget (AgentLifecycleMiddleware normally
    does this; the minimal agent omits it)."""
    ctx = DiagnosisRunContext(case_id="TEST-RECURSION")
    ctx.ctx_budget = ContextBudget()
    ctx.ctx_budget.start_timer()
    set_run_context(ctx)
    return ctx


class TestRecursionBudget:
    async def test_old_limit_80_recurses_when_never_stopping(self) -> None:
        """Documents the §7.1 regression: with the 5-steps/iter structure,
        recursion_limit=80 (= 16*5) hits step 81 (the 17th before_model where
        BudgetGuard would fire) -> GraphRecursionError, BudgetGuard never
        triggers."""
        _fresh_ctx()
        try:
            agent = _build_loop_agent()
            with pytest.raises(GraphRecursionError):
                await agent.ainvoke(
                    {"messages": [HumanMessage(content="go")]},
                    config={"recursion_limit": 80},
                )
        finally:
            clear_run_context()

    async def test_configured_limit_stops_gracefully_via_budget_guard(self) -> None:
        """With RECURSION_LIMIT the never-stopping agent is stopped GRACEFULLY
        by BudgetGuard (no GraphRecursionError) at MAX_MODEL_CALLS. Contract:
        recursion_limit is a safety net ABOVE the budget guard, never the
        binding constraint."""
        ctx = _fresh_ctx()
        try:
            agent = _build_loop_agent()
            result = await agent.ainvoke(
                {"messages": [HumanMessage(content="go")]},
                config={"recursion_limit": RECURSION_LIMIT},
            )
            # no exception raised; BudgetGuard fired jump_to=end
            assert ctx.budget_exhausted is True
            assert ctx.model_call_count == MAX_MODEL_CALLS + 1  # incremented, then jumped
            assert isinstance(result, dict) and "messages" in result
        finally:
            clear_run_context()
