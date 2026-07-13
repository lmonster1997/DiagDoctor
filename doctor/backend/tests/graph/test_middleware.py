"""Unit tests for the 5 create_agent middlewares + run_context.

Each middleware's hooks are tested in isolation with fake state + runtime +
ToolCallRequest. The end-to-end wiring (create_agent + all middlewares) is
covered by TestForcedCallWiredIntoNode in test_forced_final_json_call.py.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.engine.budget.constants import (
    MAX_TIME_SECONDS,
    MAX_TOKENS_BUDGET,
    MAX_TOOL_CALLS,
)
from src.engine.context.budget import ContextBudget
from src.engine.middleware import (
    BudgetGuardMiddleware,
    DiagnosisRunContext,
    ForcedFinalCallMiddleware,
    LangfuseTracingMiddleware,
    ToolDedupMiddleware,
    ToolTruncationMiddleware,
    clear_run_context,
    get_run_context,
    set_run_context,
)

# ═════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════


def _make_tool_call_request(
    tool_name: str = "echo",
    tool_args: dict[str, Any] | None = None,
    tool_call_id: str = "tc1",
    tool: Any = None,
) -> SimpleNamespace:
    """Build a minimal stand-in for langgraph's ToolCallRequest."""
    if tool_args is None:
        tool_args = {"text": "hi"}
    fake_tool = SimpleNamespace(name=tool_name) if tool is None else tool
    return SimpleNamespace(
        tool_call={"name": tool_name, "args": tool_args, "id": tool_call_id},
        tool=fake_tool,
        state={},
        runtime=None,
    )


@pytest.fixture
def run_ctx() -> DiagnosisRunContext:
    """Fresh run context set into the ContextVar for each test."""
    ctx = DiagnosisRunContext(case_id="TEST-001")
    set_run_context(ctx)
    yield ctx
    clear_run_context()


@pytest.fixture
def no_langfuse_ctx() -> DiagnosisRunContext:
    """Run context with langfuse_handler=None (graceful-degradation path)."""
    ctx = DiagnosisRunContext(case_id="TEST-NO-LF")
    set_run_context(ctx)
    yield ctx
    clear_run_context()


# ═════════════════════════════════════════════════════════════════════
# run_context
# ═════════════════════════════════════════════════════════════════════


class TestRunContext:
    def test_set_get_clear_roundtrip(self) -> None:
        ctx = DiagnosisRunContext(case_id="X")
        set_run_context(ctx)
        assert get_run_context() is ctx
        clear_run_context()
        with pytest.raises(LookupError):
            get_run_context()

    def test_get_raises_when_not_set(self) -> None:
        clear_run_context()
        with pytest.raises(LookupError, match="DiagnosisRunContext not set"):
            get_run_context()

    def test_default_fields(self) -> None:
        ctx = DiagnosisRunContext()
        assert ctx.case_id == ""
        assert ctx.langfuse_handler is None
        assert ctx.call_history == []
        assert ctx.model_call_count == 0
        assert ctx.budget_exhausted is False
        assert ctx.forced_call_triggered is False
        assert isinstance(ctx.ctx_budget, ContextBudget)


# ═════════════════════════════════════════════════════════════════════
# LangfuseTracingMiddleware
# ═════════════════════════════════════════════════════════════════════


class TestLangfuseTracingMiddleware:
    async def test_before_agent_inits_budget_with_system_prompt_and_evidence(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        run_ctx.system_prompt_text = "You are a diagnosis agent." * 50
        run_ctx.evidence_text = "signal: error in tasks.py" * 20
        mw = LangfuseTracingMiddleware()
        await mw.abefore_agent(state={}, runtime=None)
        # Budget should have nonzero tokens from system prompt + evidence
        assert run_ctx.ctx_budget.system_prompt_tokens > 0
        assert run_ctx.ctx_budget.evidence_tokens > 0
        assert run_ctx.ctx_budget.total_used > 0
        # call_history + counters reset
        assert run_ctx.call_history == []
        assert run_ctx.model_call_count == 0
        assert run_ctx.budget_exhausted is False

    async def test_before_agent_starts_trace_when_handler_present(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        handler = MagicMock()
        run_ctx.langfuse_handler = handler
        run_ctx.langfuse_trace_id = "trace-123"
        run_ctx.evidence_text = "some evidence"
        mw = LangfuseTracingMiddleware()
        await mw.abefore_agent(state={}, runtime=None)
        handler.start_trace.assert_called_once()
        _, kwargs = handler.start_trace.call_args
        assert kwargs["trace_id"] == "trace-123"

    async def test_before_agent_no_handler_does_not_raise(
        self, no_langfuse_ctx: DiagnosisRunContext
    ) -> None:
        mw = LangfuseTracingMiddleware()
        # Should not raise even with langfuse_handler=None
        await mw.abefore_agent(state={}, runtime=None)

    async def test_before_agent_handler_exc_is_suppressed(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        """Langfuse outage must not block diagnosis (graceful degradation)."""
        handler = MagicMock()
        handler.start_trace.side_effect = RuntimeError("langfuse down")
        run_ctx.langfuse_handler = handler
        mw = LangfuseTracingMiddleware()
        await mw.abefore_agent(state={}, runtime=None)  # must not raise

    async def test_wrap_tool_call_records_span_and_accounts_tokens(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        handler = MagicMock()
        run_ctx.langfuse_handler = handler
        run_ctx.model_call_count = 3
        mw = LangfuseTracingMiddleware()
        inner_result = ToolMessage(content="raw result text", tool_call_id="tc1", name="echo")
        fake_handler = AsyncMock(return_value=inner_result)
        result = await mw.awrap_tool_call(_make_tool_call_request(), fake_handler)
        # Tool token accounting
        assert run_ctx.ctx_budget.tool_calls == 1
        assert run_ctx.ctx_budget.tool_result_tokens > 0
        # Span recorded
        handler.record_tool_span.assert_called_once()
        span_kwargs = handler.record_tool_span.call_args.kwargs
        assert span_kwargs["tool_name"] == "echo"
        assert span_kwargs["result"] == "raw result text"
        assert span_kwargs["iteration"] == 3
        # Result passed through unchanged
        assert result is inner_result

    async def test_after_agent_no_handler_does_not_raise(
        self, no_langfuse_ctx: DiagnosisRunContext
    ) -> None:
        # end_trace is owned by the node (_finalize_langfuse_trace), not this
        # middleware — so there's no aafter_agent hook to test here. This stub
        # keeps the test class structure; the node-level end_trace behavior is
        # covered by TestForcedCallWiredIntoNode in test_forced_final_json_call.py.
        mw = LangfuseTracingMiddleware()
        # Middleware has no aafter_agent — nothing to call. Just assert the
        # middleware instance is usable.
        assert mw is not None

    async def test_wrap_model_call_attaches_handler_to_model_only(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        """awrap_model_call wraps request.model with .with_config(callbacks=[handler])
        so the Langfuse handler fires for THIS LLM call only (not the ToolNode,
        which would double-record tools). Verified by checking the model passed
        to the inner handler carries the callback."""
        handler = MagicMock()
        run_ctx.langfuse_handler = handler
        fake_model = MagicMock()
        wrapped_model = MagicMock()
        fake_model.with_config = MagicMock(return_value=wrapped_model)
        request = SimpleNamespace(model=fake_model, messages=[], system_message=None)
        captured: list[Any] = []

        async def fake_handler(req: Any) -> Any:
            captured.append(req.model)
            return "model-result"

        mw = LangfuseTracingMiddleware()
        result = await mw.awrap_model_call(request, fake_handler)
        # with_config called with the handler
        fake_model.with_config.assert_called_once_with({"callbacks": [handler]})
        # inner handler received the WRAPPED model
        assert captured == [wrapped_model]
        assert result == "model-result"

    async def test_wrap_model_call_no_handler_passes_through(
        self, no_langfuse_ctx: DiagnosisRunContext
    ) -> None:
        """When langfuse_handler is None, awrap_model_call does not wrap the model."""
        fake_model = MagicMock()
        request = SimpleNamespace(model=fake_model, messages=[], system_message=None)

        async def fake_handler(req: Any) -> Any:
            return "ok"

        mw = LangfuseTracingMiddleware()
        await mw.awrap_model_call(request, fake_handler)
        fake_model.with_config.assert_not_called()


# ═════════════════════════════════════════════════════════════════════
# BudgetGuardMiddleware
# ═════════════════════════════════════════════════════════════════════


class TestBudgetGuardMiddleware:
    async def test_before_model_increments_count_and_ticks(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        mw = BudgetGuardMiddleware()
        starting_iter = run_ctx.ctx_budget.iteration
        result = await mw.abefore_model(state={}, runtime=None)
        assert result is None
        assert run_ctx.model_call_count == 1
        assert run_ctx.ctx_budget.iteration == starting_iter + 1

    async def test_before_model_jumps_to_end_on_iteration_cap(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        run_ctx.model_call_count = MAX_TOOL_CALLS  # already at cap; next call is over
        mw = BudgetGuardMiddleware()
        result = await mw.abefore_model(state={}, runtime=None)
        assert result == {"jump_to": "end"}
        assert run_ctx.budget_exhausted is True
        assert run_ctx.model_call_count == MAX_TOOL_CALLS + 1

    async def test_before_model_jumps_to_end_on_token_cap(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        run_ctx.ctx_budget.system_prompt_tokens = MAX_TOKENS_BUDGET  # total_used >= cap
        mw = BudgetGuardMiddleware()
        result = await mw.abefore_model(state={}, runtime=None)
        assert result == {"jump_to": "end"}
        assert run_ctx.budget_exhausted is True

    async def test_before_model_jumps_to_end_on_time_cap(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        # Force elapsed_seconds > MAX_TIME_SECONDS by backdating the timer
        run_ctx.ctx_budget.started_at_monotonic = time.monotonic() - (MAX_TIME_SECONDS + 5)
        mw = BudgetGuardMiddleware()
        result = await mw.abefore_model(state={}, runtime=None)
        assert result == {"jump_to": "end"}
        assert run_ctx.budget_exhausted is True

    async def test_after_model_accounts_agent_reasoning_tokens(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        state = {"messages": [HumanMessage(content="q"), AIMessage(content="thinking about the bug")]}
        before = run_ctx.ctx_budget.agent_reasoning_tokens
        mw = BudgetGuardMiddleware()
        result = await mw.aafter_model(state=state, runtime=None)
        assert result is None
        assert run_ctx.ctx_budget.agent_reasoning_tokens > before

    async def test_after_model_no_ai_message_does_not_raise(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        state = {"messages": [HumanMessage(content="q")]}
        mw = BudgetGuardMiddleware()
        await mw.aafter_model(state=state, runtime=None)  # no AIMessage — skip


# ═════════════════════════════════════════════════════════════════════
# ToolDedupMiddleware
# ═════════════════════════════════════════════════════════════════════


class TestToolDedupMiddleware:
    async def test_first_call_appends_to_history_and_invokes_handler(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        handler = AsyncMock(return_value=ToolMessage(content="r", tool_call_id="tc1", name="echo"))
        mw = ToolDedupMiddleware()
        request = _make_tool_call_request("echo", {"text": "hi"}, "tc1")
        await mw.awrap_tool_call(request, handler)
        handler.assert_awaited_once()
        assert len(run_ctx.call_history) == 1
        assert run_ctx.call_history[0][0] == "echo"

    async def test_duplicate_call_returns_skip_message_without_invoking_handler(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        handler = AsyncMock(return_value=ToolMessage(content="r", tool_call_id="tc1", name="echo"))
        lf_handler = MagicMock()
        run_ctx.langfuse_handler = lf_handler
        mw = ToolDedupMiddleware()
        request = _make_tool_call_request("echo", {"text": "hi"}, "tc1")

        # First call — executes
        await mw.awrap_tool_call(request, handler)
        # Second identical call — should be skipped
        result = await mw.awrap_tool_call(request, handler)

        # Handler invoked only once (first call)
        handler.assert_awaited_once()
        # Result is a [跳过] ToolMessage
        assert isinstance(result, ToolMessage)
        assert "跳过" in str(result.content)
        assert result.tool_call_id == "tc1"
        # record_tool_skipped called for the dup
        lf_handler.record_tool_skipped.assert_called_once()
        # call_history NOT doubled (dup not appended)
        assert len(run_ctx.call_history) == 1

    async def test_different_args_not_treated_as_dup(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        handler = AsyncMock(return_value=ToolMessage(content="r", tool_call_id="tc", name="echo"))
        mw = ToolDedupMiddleware()
        await mw.awrap_tool_call(_make_tool_call_request("echo", {"text": "a"}, "tc1"), handler)
        await mw.awrap_tool_call(_make_tool_call_request("echo", {"text": "b"}, "tc2"), handler)
        assert handler.await_count == 2
        assert len(run_ctx.call_history) == 2


# ═════════════════════════════════════════════════════════════════════
# ToolTruncationMiddleware
# ═════════════════════════════════════════════════════════════════════


class TestToolTruncationMiddleware:
    async def test_short_result_passes_through_unchanged(self) -> None:
        short = ToolMessage(content="short result", tool_call_id="tc1", name="get_file_content")
        handler = AsyncMock(return_value=short)
        mw = ToolTruncationMiddleware()
        result = await mw.awrap_tool_call(
            _make_tool_call_request("get_file_content", {"path": "a.py"}, "tc1"), handler
        )
        assert result is short

    async def test_long_result_is_truncated(self) -> None:
        # get_file_content cap is 8000 chars — produce something bigger.
        # NOTE: truncation is gated by settings.tool_result_truncation_enabled
        # (default False); enable it for this test.
        long_content = "x" * 20_000
        long_msg = ToolMessage(content=long_content, tool_call_id="tc1", name="get_file_content")
        handler = AsyncMock(return_value=long_msg)
        mw = ToolTruncationMiddleware()
        with patch("src.graph.context_engine.settings") as mock_settings:
            mock_settings.tool_result_truncation_enabled = True
            result = await mw.awrap_tool_call(
                _make_tool_call_request("get_file_content", {"path": "a.py"}, "tc1"), handler
            )
        assert isinstance(result, ToolMessage)
        assert len(str(result.content)) < len(long_content)
        assert len(str(result.content)) <= 8000 + 200  # cap + small slack for key-line retention

    async def test_handler_exception_returns_truncated_error_message(self) -> None:
        handler = AsyncMock(side_effect=RuntimeError("tool blew up"))
        mw = ToolTruncationMiddleware()
        with patch("src.graph.context_engine.settings") as mock_settings:
            mock_settings.tool_result_truncation_enabled = True
            result = await mw.awrap_tool_call(
                _make_tool_call_request("code_search", {"query": "x"}, "tc1"), handler
            )
        assert isinstance(result, ToolMessage)
        assert "工具执行错误" in str(result.content)
        assert "tool blew up" in str(result.content)
        assert result.tool_call_id == "tc1"

    async def test_truncation_disabled_passes_through(self) -> None:
        """When settings.tool_result_truncation_enabled=False (default), the
        middleware does NOT truncate — preserves full tool output."""
        long_content = "x" * 20_000
        long_msg = ToolMessage(content=long_content, tool_call_id="tc1", name="get_file_content")
        handler = AsyncMock(return_value=long_msg)
        mw = ToolTruncationMiddleware()
        with patch("src.graph.context_engine.settings") as mock_settings:
            mock_settings.tool_result_truncation_enabled = False
            result = await mw.awrap_tool_call(
                _make_tool_call_request("get_file_content", {"path": "a.py"}, "tc1"), handler
            )
        assert isinstance(result, ToolMessage)
        assert len(str(result.content)) == 20_000


# ═════════════════════════════════════════════════════════════════════
# ForcedFinalCallMiddleware
# ═════════════════════════════════════════════════════════════════════


class TestForcedFinalCallMiddleware:
    async def test_skips_when_last_ai_has_json(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        state = {"messages": [AIMessage(content='{"primary_category":"logic"}')]}
        mw = ForcedFinalCallMiddleware()
        with patch("src.llm_factory.get_llm_for_role") as mock_llm:
            result = await mw.aafter_agent(state=state, runtime=None)
        assert result is None
        mock_llm.assert_not_called()
        assert run_ctx.forced_call_triggered is False

    async def test_skips_when_budget_blown(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        run_ctx.ctx_budget.system_prompt_tokens = MAX_TOKENS_BUDGET
        state = {"messages": [AIMessage(content="narrative, no json")]}
        mw = ForcedFinalCallMiddleware()
        with patch("src.llm_factory.get_llm_for_role") as mock_llm:
            result = await mw.aafter_agent(state=state, runtime=None)
        assert result is None
        mock_llm.assert_not_called()

    async def test_skips_when_messages_empty(self, run_ctx: DiagnosisRunContext) -> None:
        mw = ForcedFinalCallMiddleware()
        with patch("src.llm_factory.get_llm_for_role") as mock_llm:
            result = await mw.aafter_agent(state={"messages": []}, runtime=None)
        assert result is None
        mock_llm.assert_not_called()

    async def test_triggers_forced_call_when_no_json_and_budget_ok(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        # Last AI is narrative (no JSON), budget fine → trigger
        state = {"messages": [HumanMessage(content="q"), AIMessage(content="narrative conclusion")]}
        forced_msg = AIMessage(content='{"primary_category":"logic","confidence":0.8}')
        mw = ForcedFinalCallMiddleware()
        with patch("src.llm_factory.get_llm_for_role") as mock_llm_role, \
             patch(
                 "src.graph.nodes.diagnosis_agent.middleware.forced_call._forced_final_json_call",
                 new=AsyncMock(return_value=forced_msg),
             ) as mock_forced:
            result = await mw.aafter_agent(state=state, runtime=None)
        assert result == {"messages": [forced_msg]}
        assert run_ctx.forced_call_triggered is True
        mock_llm_role.assert_called_once_with("diagnosis")
        mock_forced.assert_awaited_once()
        # natural_stop=True because last AIMessage has no tool_calls
        forced_kwargs = mock_forced.call_args.kwargs
        assert forced_kwargs["natural_stop"] is True

    async def test_natural_stop_false_when_last_ai_has_tool_calls(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        cap_msg = AIMessage(content="")
        cap_msg.tool_calls = [{"name": "code_search", "args": {}, "id": "x"}]  # type: ignore[attr-defined]
        state = {"messages": [cap_msg]}
        forced_msg = AIMessage(content='{"primary_category":"logic"}')
        mw = ForcedFinalCallMiddleware()
        with patch("src.llm_factory.get_llm_for_role"), \
             patch(
                 "src.graph.nodes.diagnosis_agent.middleware.forced_call._forced_final_json_call",
                 new=AsyncMock(return_value=forced_msg),
             ) as mock_forced:
            await mw.aafter_agent(state=state, runtime=None)
        forced_kwargs = mock_forced.call_args.kwargs
        assert forced_kwargs["natural_stop"] is False

    async def test_forced_call_returns_none_falls_through(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        """If _forced_final_json_call itself fails (returns None), middleware
        returns None so the node's existing fallback report path runs."""
        state = {"messages": [AIMessage(content="narrative")]}
        mw = ForcedFinalCallMiddleware()
        with patch("src.llm_factory.get_llm_for_role"), \
             patch(
                 "src.graph.nodes.diagnosis_agent.middleware.forced_call._forced_final_json_call",
                 new=AsyncMock(return_value=None),
             ):
            result = await mw.aafter_agent(state=state, runtime=None)
        assert result is None
        # forced_call_triggered still True (the trigger happened, just failed)
        assert run_ctx.forced_call_triggered is True

    async def test_invoke_config_includes_langfuse_callbacks(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        """When a Langfuse handler is present, it's attached to the forced call
        config so the forced LLM call gets traced."""
        lf = MagicMock()
        run_ctx.langfuse_handler = lf
        state = {"messages": [AIMessage(content="narrative")]}
        forced_msg = AIMessage(content='{"primary_category":"logic"}')
        mw = ForcedFinalCallMiddleware()
        with patch("src.llm_factory.get_llm_for_role"), \
             patch(
                 "src.graph.nodes.diagnosis_agent.middleware.forced_call._forced_final_json_call",
                 new=AsyncMock(return_value=forced_msg),
             ) as mock_forced:
            await mw.aafter_agent(state=state, runtime=None)
        forced_kwargs = mock_forced.call_args.kwargs
        assert forced_kwargs["invoke_config"].get("callbacks") == [lf]
        assert forced_kwargs["langfuse_handler"] is lf
