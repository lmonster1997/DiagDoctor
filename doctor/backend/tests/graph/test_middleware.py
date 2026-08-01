"""Unit tests for the 5 create_agent middlewares + run_context.

Each middleware's hooks are tested in isolation with fake state + runtime +
ToolCallRequest. The end-to-end wiring (create_agent + all middlewares) is
covered by TestForcedCallWiredIntoNode in test_forced_final_json_call.py.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime

from src.engine.budget.constants import (
    MAX_MODEL_CALLS,
    MAX_TIME_SECONDS,
    MAX_TOKENS_BUDGET,
)
from src.engine.context.budget import ContextBudget
from src.engine.middleware import (
    AgentLifecycleMiddleware,
    BudgetGuardMiddleware,
    ContextElisionMiddleware,
    DiagnosisRunContext,
    ForcedFinalCallMiddleware,
    LangfuseTracingMiddleware,
    ToolDedupMiddleware,
    ToolTruncationMiddleware,
)

# ═════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════


def _make_runtime(ctx: DiagnosisRunContext | None = None) -> Runtime:
    """Build a Runtime whose ``.context`` is ``ctx`` (or None). Stand-in for
    langgraph's per-invocation Runtime so middlewares can read ``runtime.context``."""
    return Runtime(context=ctx)


def _make_tool_call_request(
    tool_name: str = "echo",
    tool_args: dict[str, Any] | None = None,
    tool_call_id: str = "tc1",
    tool: Any = None,
    runtime: Any = None,
) -> SimpleNamespace:
    """Build a minimal stand-in for langgraph's ToolCallRequest."""
    if tool_args is None:
        tool_args = {"text": "hi"}
    fake_tool = SimpleNamespace(name=tool_name) if tool is None else tool
    return SimpleNamespace(
        tool_call={"name": tool_name, "args": tool_args, "id": tool_call_id},
        tool=fake_tool,
        state={},
        runtime=runtime if runtime is not None else _make_runtime(),
    )


@pytest.fixture
def run_ctx() -> DiagnosisRunContext:
    """Fresh run context for each test (passed via Runtime.context)."""
    ctx = DiagnosisRunContext(case_id="TEST-001")
    yield ctx


@pytest.fixture
def no_langfuse_ctx() -> DiagnosisRunContext:
    """Run context with langfuse_handler=None (graceful-degradation path)."""
    ctx = DiagnosisRunContext(case_id="TEST-NO-LF")
    yield ctx


# ═════════════════════════════════════════════════════════════════════
# run_context
# ═════════════════════════════════════════════════════════════════════


class TestRunContext:
    def test_default_fields(self) -> None:
        ctx = DiagnosisRunContext()
        assert ctx.case_id == ""
        assert ctx.langfuse_handler is None
        assert ctx.call_history == {}
        assert ctx.elided_tool_call_ids == set()
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
        mw = AgentLifecycleMiddleware()
        await mw.abefore_agent(state={}, runtime=_make_runtime(run_ctx))
        # Budget should have nonzero tokens from system prompt + evidence
        assert run_ctx.ctx_budget.system_prompt_tokens > 0
        assert run_ctx.ctx_budget.evidence_tokens > 0
        assert run_ctx.ctx_budget.total_used > 0
        # call_history + counters reset
        assert run_ctx.call_history == {}
        assert run_ctx.elided_tool_call_ids == set()
        assert run_ctx.model_call_count == 0
        assert run_ctx.budget_exhausted is False

    async def test_before_agent_no_handler_does_not_raise(
        self, no_langfuse_ctx: DiagnosisRunContext
    ) -> None:
        mw = LangfuseTracingMiddleware()
        # Should not raise even with langfuse_handler=None
        await mw.abefore_agent(state={}, runtime=_make_runtime(no_langfuse_ctx))

    async def test_wrap_tool_call_records_span(self, run_ctx: DiagnosisRunContext) -> None:
        handler = MagicMock()
        run_ctx.langfuse_handler = handler
        run_ctx.model_call_count = 3
        mw = LangfuseTracingMiddleware()
        inner_result = ToolMessage(content="raw result text", tool_call_id="tc1", name="echo")
        fake_handler = AsyncMock(return_value=inner_result)
        result = await mw.awrap_tool_call(_make_tool_call_request(runtime=_make_runtime(run_ctx)), fake_handler)
        # Span recorded
        handler.record_tool_span.assert_called_once()
        span_kwargs = handler.record_tool_span.call_args.kwargs
        assert span_kwargs["tool_name"] == "echo"
        assert span_kwargs["result"] == "raw result text"
        assert span_kwargs["iteration"] == 3
        # Result passed through unchanged
        assert result is inner_result

    async def test_after_agent_does_not_end_trace(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        # Regression: aafter_agent must NOT call end_trace. The trace output
        # (diagnosis report) is owned by diagnosis_agent_node's
        # _finalize_langfuse_trace, which runs AFTER agent.ainvoke returns.
        # aafter_agent runs BEFORE that (inside agent.ainvoke). If it calls
        # end_trace() here with no output, it resets handler._trace_id to None,
        # so the node's later end_trace(output_data=report) hits
        # ``if self._trace_id is None: return`` and the report is lost ->
        # trace output stays empty (the exact bug this test guards against).
        handler = MagicMock()
        run_ctx.langfuse_handler = handler
        mw = LangfuseTracingMiddleware()
        await mw.aafter_agent(state={}, runtime=_make_runtime(run_ctx))
        handler.end_trace.assert_not_called()


# ═════════════════════════════════════════════════════════════════════
# BudgetGuardMiddleware
# ═════════════════════════════════════════════════════════════════════


class TestBudgetGuardMiddleware:
    async def test_before_model_increments_count_and_ticks(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        mw = BudgetGuardMiddleware()
        starting_iter = run_ctx.ctx_budget.iteration
        result = await mw.abefore_model(state={}, runtime=_make_runtime(run_ctx))
        assert result is None
        assert run_ctx.model_call_count == 1
        assert run_ctx.ctx_budget.iteration == starting_iter + 1

    async def test_before_model_jumps_to_end_on_iteration_cap(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        run_ctx.model_call_count = MAX_MODEL_CALLS  # already at cap; next call is over
        mw = BudgetGuardMiddleware()
        result = await mw.abefore_model(state={}, runtime=_make_runtime(run_ctx))
        assert result == {"jump_to": "end"}
        assert run_ctx.budget_exhausted is True
        assert run_ctx.model_call_count == MAX_MODEL_CALLS + 1

    async def test_before_model_jumps_to_end_on_token_cap(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        run_ctx.ctx_budget.system_prompt_tokens = MAX_TOKENS_BUDGET  # total_used >= cap
        mw = BudgetGuardMiddleware()
        result = await mw.abefore_model(state={}, runtime=_make_runtime(run_ctx))
        assert result == {"jump_to": "end"}
        assert run_ctx.budget_exhausted is True

    async def test_before_model_jumps_to_end_on_time_cap(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        # Force elapsed_seconds > MAX_TIME_SECONDS by backdating the timer
        run_ctx.ctx_budget.started_at_monotonic = time.monotonic() - (MAX_TIME_SECONDS + 5)
        mw = BudgetGuardMiddleware()
        result = await mw.abefore_model(state={}, runtime=_make_runtime(run_ctx))
        assert result == {"jump_to": "end"}
        assert run_ctx.budget_exhausted is True

    async def test_after_model_accounts_agent_reasoning_tokens(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        state = {
            "messages": [HumanMessage(content="q"), AIMessage(content="thinking about the bug")]
        }
        before = run_ctx.ctx_budget.agent_reasoning_tokens
        mw = BudgetGuardMiddleware()
        result = await mw.aafter_model(state=state, runtime=_make_runtime(run_ctx))
        assert result is None
        assert run_ctx.ctx_budget.agent_reasoning_tokens > before

    async def test_after_model_no_ai_message_does_not_raise(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        state = {"messages": [HumanMessage(content="q")]}
        mw = BudgetGuardMiddleware()
        await mw.aafter_model(state=state, runtime=_make_runtime(run_ctx))  # no AIMessage — skip

    async def test_after_model_records_real_input_tokens(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        """§7.3: aafter_model captures usage_metadata.input_tokens (peak context)."""
        msg = AIMessage(
            content="ok",
            usage_metadata={"input_tokens": 5000, "output_tokens": 200, "total_tokens": 5200},
        )
        mw = BudgetGuardMiddleware()
        await mw.aafter_model(state={"messages": [msg]}, runtime=_make_runtime(run_ctx))
        assert run_ctx.ctx_budget.real_input_tokens == 5000
        # total_used follows real_input_tokens once a call has happened
        assert run_ctx.ctx_budget.total_used == 5000

    async def test_after_model_real_input_tokens_tracks_peak(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        """real_input_tokens is the peak across calls, not the latest."""
        mw = BudgetGuardMiddleware()
        for inp in (5000, 3000, 9000):
            msg = AIMessage(
                content="x",
                usage_metadata={"input_tokens": inp, "output_tokens": 1, "total_tokens": inp + 1},
            )
            await mw.aafter_model(state={"messages": [msg]}, runtime=_make_runtime(run_ctx))
        assert run_ctx.ctx_budget.real_input_tokens == 9000

    async def test_real_input_tokens_drive_token_gate(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        """Token gate fires on real_input_tokens >= MAX_TOKENS_BUDGET."""
        run_ctx.ctx_budget.record_real_usage(MAX_TOKENS_BUDGET)
        mw = BudgetGuardMiddleware()
        result = await mw.abefore_model(state={}, runtime=_make_runtime(run_ctx))
        assert result == {"jump_to": "end"}
        assert run_ctx.budget_exhausted is True

    async def test_huge_tool_result_does_not_inflate_token_gate(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        """§6.1 split-brain regression: a pre-truncation huge tool result must
        NOT inflate total_used / trip the token gate. The gate uses
        real_input_tokens (post-truncation); tool_result_tokens is telemetry-only.
        """
        huge = "x" * 600_000  # would far exceed MAX_TOKENS_BUDGET if it fed the gate
        handler = AsyncMock(
            return_value=ToolMessage(content=huge, tool_call_id="tc1", name="search_observability")
        )
        mw = BudgetGuardMiddleware()
        await mw.awrap_tool_call(
            _make_tool_call_request("search_observability", {}, "tc1", runtime=_make_runtime(run_ctx)), handler
        )
        # tool_result_tokens recorded (telemetry) but gate's total_used NOT inflated
        assert run_ctx.ctx_budget.tool_result_tokens > 0
        assert run_ctx.ctx_budget.total_used == 0
        # gate does not fire despite the huge tool result
        run_ctx.model_call_count = 0  # avoid iteration cap
        result = await mw.abefore_model(state={}, runtime=_make_runtime(run_ctx))
        assert result is None
        assert run_ctx.budget_exhausted is False

    # ── §7.2 record_hypothesis budget exemption ──────────────────────

    async def test_record_hypothesis_tool_call_exempt_from_tool_calls(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        """record_hypothesis is instrumentation; awrap_tool_call must NOT count
        it toward tool_calls (would eat the diagnostic MAX_MODEL_CALLS cap)."""
        handler = AsyncMock(
            return_value=ToolMessage(content="已记录", tool_call_id="rh1", name="record_hypothesis")
        )
        mw = BudgetGuardMiddleware()
        await mw.awrap_tool_call(
            _make_tool_call_request(
                "record_hypothesis",
                {"hypothesis": "h", "status": "excluded", "evidence": "e", "refuted": True},
                "rh1",
                runtime=_make_runtime(run_ctx),
            ),
            handler,
        )
        assert run_ctx.ctx_budget.tool_calls == 0  # not counted
        assert run_ctx.ctx_budget.tool_result_tokens == 0  # ack not counted either

        # a diagnostic tool call IS counted
        handler2 = AsyncMock(
            return_value=ToolMessage(content="result", tool_call_id="tc1", name="search_observability")
        )
        await mw.awrap_tool_call(_make_tool_call_request("search_observability", {}, "tc1", runtime=_make_runtime(run_ctx)), handler2)
        assert run_ctx.ctx_budget.tool_calls == 1

    async def test_pure_record_hypothesis_turn_decrements_model_call_count(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        """A turn whose tool_calls are ALL record_hypothesis is pure
        instrumentation; aafter_model decrements model_call_count so it doesn't
        advance the diagnostic MAX_MODEL_CALLS cap (§5.3 calibration preserved)."""
        run_ctx.model_call_count = 5  # abefore_model already incremented for this turn
        msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "record_hypothesis",
                    "args": {"hypothesis": "h", "status": "excluded"},
                    "id": "rh1",
                    "type": "tool_call",
                }
            ],
        )
        mw = BudgetGuardMiddleware()
        await mw.aafter_model(state={"messages": [msg]}, runtime=_make_runtime(run_ctx))
        assert run_ctx.model_call_count == 4  # decremented back

    async def test_mixed_record_and_diagnostic_turn_not_decremented(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        """A turn mixing record_hypothesis + a diagnostic tool is NOT pure
        instrumentation; model_call_count stands (it's a real diagnostic step)."""
        run_ctx.model_call_count = 5
        msg = AIMessage(
            content="",
            tool_calls=[
                {"name": "record_hypothesis", "args": {}, "id": "rh1", "type": "tool_call"},
                {"name": "search_observability", "args": {}, "id": "tc1", "type": "tool_call"},
            ],
        )
        mw = BudgetGuardMiddleware()
        await mw.aafter_model(state={"messages": [msg]}, runtime=_make_runtime(run_ctx))
        assert run_ctx.model_call_count == 5  # not decremented

    async def test_record_hypothesis_turn_still_records_token_usage(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        """Token budget is real (the turn consumed tokens); only model_call_count
        is exempted for pure record turns -- tokens are a real resource cost."""
        run_ctx.model_call_count = 1
        msg = AIMessage(
            content="",
            usage_metadata={"input_tokens": 4000, "output_tokens": 10, "total_tokens": 4010},
            tool_calls=[
                {"name": "record_hypothesis", "args": {}, "id": "rh1", "type": "tool_call"}
            ],
        )
        mw = BudgetGuardMiddleware()
        await mw.aafter_model(state={"messages": [msg]}, runtime=_make_runtime(run_ctx))
        assert run_ctx.ctx_budget.real_input_tokens == 4000  # token usage recorded (real cost)
        assert run_ctx.model_call_count == 0  # but the diagnostic turn-count was exempted


# ═════════════════════════════════════════════════════════════════════
# ToolDedupMiddleware
# ═════════════════════════════════════════════════════════════════════


class TestToolDedupMiddleware:
    async def test_first_call_appends_to_history_and_invokes_handler(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        handler = AsyncMock(return_value=ToolMessage(content="r", tool_call_id="tc1", name="echo"))
        mw = ToolDedupMiddleware()
        request = _make_tool_call_request("echo", {"text": "hi"}, "tc1", runtime=_make_runtime(run_ctx))
        await mw.awrap_tool_call(request, handler)
        handler.assert_awaited_once()
        assert len(run_ctx.call_history) == 1
        key = ("echo", json.dumps({"text": "hi"}, sort_keys=True, default=str))
        assert run_ctx.call_history[key] == "tc1"

    async def test_duplicate_call_returns_skip_message_without_invoking_handler(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        handler = AsyncMock(return_value=ToolMessage(content="r", tool_call_id="tc1", name="echo"))
        lf_handler = MagicMock()
        run_ctx.langfuse_handler = lf_handler
        mw = ToolDedupMiddleware()
        request = _make_tool_call_request("echo", {"text": "hi"}, "tc1", runtime=_make_runtime(run_ctx))

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

    async def test_different_args_not_treated_as_dup(self, run_ctx: DiagnosisRunContext) -> None:
        handler = AsyncMock(return_value=ToolMessage(content="r", tool_call_id="tc", name="echo"))
        mw = ToolDedupMiddleware()
        await mw.awrap_tool_call(_make_tool_call_request("echo", {"text": "a"}, "tc1", runtime=_make_runtime(run_ctx)), handler)
        await mw.awrap_tool_call(_make_tool_call_request("echo", {"text": "b"}, "tc2", runtime=_make_runtime(run_ctx)), handler)
        assert handler.await_count == 2
        assert len(run_ctx.call_history) == 2

    async def test_duplicate_after_elision_allows_refetch(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        """elision-aware dedup: prior result aged out -> identical re-call is a
        legitimate re-fetch, allowed through (not skipped)."""
        handler = AsyncMock(return_value=ToolMessage(content="r", tool_call_id="tc2", name="echo"))
        mw = ToolDedupMiddleware()
        # first call executes, records call_key -> "tc1"
        await mw.awrap_tool_call(_make_tool_call_request("echo", {"text": "hi"}, "tc1", runtime=_make_runtime(run_ctx)), handler)
        # simulate ContextElision having aged out tc1's result
        run_ctx.elided_tool_call_ids.add("tc1")
        # second identical call (new id tc2) -> allowed re-fetch
        result = await mw.awrap_tool_call(_make_tool_call_request("echo", {"text": "hi"}, "tc2", runtime=_make_runtime(run_ctx)), handler)
        handler.assert_awaited()
        assert handler.await_count == 2  # both calls executed
        assert isinstance(result, ToolMessage)
        assert "跳过" not in str(result.content)  # real content, not skip stub
        # mapping tracks latest result id
        key = ("echo", json.dumps({"text": "hi"}, sort_keys=True, default=str))
        assert run_ctx.call_history[key] == "tc2"

    async def test_duplicate_when_original_still_present_is_skipped(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        """elision-aware dedup: prior result still in context (not elided) ->
        identical re-call is a wasteful dup, skipped (original behaviour)."""
        handler = AsyncMock(return_value=ToolMessage(content="r", tool_call_id="tc1", name="echo"))
        mw = ToolDedupMiddleware()
        await mw.awrap_tool_call(_make_tool_call_request("echo", {"text": "hi"}, "tc1", runtime=_make_runtime(run_ctx)), handler)
        # elided_tool_call_ids stays empty -> tc1 result still in context
        result = await mw.awrap_tool_call(_make_tool_call_request("echo", {"text": "hi"}, "tc2", runtime=_make_runtime(run_ctx)), handler)
        handler.assert_awaited_once()  # only first call executed
        assert isinstance(result, ToolMessage)
        assert "跳过" in str(result.content)


# ═════════════════════════════════════════════════════════════════════
# Dedup <-> ContextElision integration (the §7.1 ↔ dedup contract)
# ═════════════════════════════════════════════════════════════════════


class TestDedupElisionIntegration:
    """Joint contract: dedup must ALLOW a re-fetch whose prior result was aged
    out by ContextElision. This is the exact scenario that caused the
    recursion-limit loop (agent re-fetches an elided file, dedup blocks the
    identical call, agent flails) - no test exercised both middlewares together
    before, so the contradiction shipped green."""

    @staticmethod
    def _code_pair(tc_id: str, args: dict[str, Any]) -> list[Any]:
        return [
            AIMessage(
                content="",
                tool_calls=[{"name": "get_file_content", "args": args, "id": tc_id, "type": "tool_call"}],
            ),
            ToolMessage(content=f"line_{tc_id}\n" + "BULK" * 200, tool_call_id=tc_id, name="get_file_content"),
        ]

    async def test_refetch_allowed_after_elision_ages_out_prior_result(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        args = {"file_path": "app/svc.py"}
        # 4 tool results so keep_recent=3 ages out tc1.
        raw: list[Any] = []
        raw += self._code_pair("tc1", args)
        raw += self._code_pair("tc2", {"file_path": "b.py"})
        raw += self._code_pair("tc3", {"file_path": "c.py"})
        raw += self._code_pair("tc4", {"file_path": "d.py"})
        messages = add_messages([], raw)

        # 1) Agent already fetched app/svc.py once (tc1) -> dedup records it.
        first_handler = AsyncMock(
            return_value=ToolMessage(content="line_tc1\n" + "BULK" * 200, tool_call_id="tc1", name="get_file_content")
        )
        dedup = ToolDedupMiddleware()
        await dedup.awrap_tool_call(_make_tool_call_request("get_file_content", args, "tc1", runtime=_make_runtime(run_ctx)), first_handler)
        first_handler.assert_awaited_once()

        # 2) Elision ages out tc1 (keep_recent=3) -> records tc1 as elided.
        elision = ContextElisionMiddleware()
        with patch("src.engine.middleware.context_elision.settings") as ms:
            ms.context_elision_enabled = True
            ms.context_elision_keep_recent = 3
            await elision.abefore_model(state={"messages": messages}, runtime=_make_runtime(run_ctx))
        assert "tc1" in run_ctx.elided_tool_call_ids

        # 3) Agent re-fetches app/svc.py (same args, new id tc5) -> dedup ALLOWS
        #    (not skipped), because tc1 was elided.
        refetch_handler = AsyncMock(
            return_value=ToolMessage(content="line_tc1\n" + "BULK" * 200, tool_call_id="tc5", name="get_file_content")
        )
        result = await dedup.awrap_tool_call(
            _make_tool_call_request("get_file_content", args, "tc5", runtime=_make_runtime(run_ctx)), refetch_handler
        )
        refetch_handler.assert_awaited_once()  # executed, not skipped
        assert isinstance(result, ToolMessage)
        assert "跳过" not in str(result.content)
        # mapping updated to the latest result's id
        key = ("get_file_content", json.dumps(args, sort_keys=True, default=str))
        assert run_ctx.call_history[key] == "tc5"


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
        # (default True); explicitly set here for test clarity.
        long_content = "x" * 20_000
        long_msg = ToolMessage(content=long_content, tool_call_id="tc1", name="get_file_content")
        handler = AsyncMock(return_value=long_msg)
        mw = ToolTruncationMiddleware()
        with patch("src.engine.context.truncation.settings") as mock_settings:
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
        with patch("src.engine.context.truncation.settings") as mock_settings:
            mock_settings.tool_result_truncation_enabled = True
            result = await mw.awrap_tool_call(
                _make_tool_call_request("code_search", {"query": "x"}, "tc1"), handler
            )
        assert isinstance(result, ToolMessage)
        assert "工具执行错误" in str(result.content)
        assert "tool blew up" in str(result.content)
        assert result.tool_call_id == "tc1"

    async def test_truncation_disabled_passes_through(self) -> None:
        """When settings.tool_result_truncation_enabled=False, the
        middleware does NOT truncate — preserves full tool output."""
        long_content = "x" * 20_000
        long_msg = ToolMessage(content=long_content, tool_call_id="tc1", name="get_file_content")
        handler = AsyncMock(return_value=long_msg)
        mw = ToolTruncationMiddleware()
        with patch("src.engine.context.truncation.settings") as mock_settings:
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
    async def test_skips_when_last_ai_has_json(self, run_ctx: DiagnosisRunContext) -> None:
        state = {"messages": [AIMessage(content='{"primary_category":"logic"}')]}
        mw = ForcedFinalCallMiddleware()
        with patch("src.llm_factory.get_llm_for_role") as mock_llm:
            result = await mw.aafter_agent(state=state, runtime=_make_runtime(run_ctx))
        assert result is None
        mock_llm.assert_not_called()
        assert run_ctx.forced_call_triggered is False

    async def test_skips_when_messages_empty(self, run_ctx: DiagnosisRunContext) -> None:
        mw = ForcedFinalCallMiddleware()
        with patch("src.llm_factory.get_llm_for_role") as mock_llm:
            result = await mw.aafter_agent(state={"messages": []}, runtime=_make_runtime(run_ctx))
        assert result is None
        mock_llm.assert_not_called()

    async def test_triggers_forced_call_when_no_json_and_budget_ok(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        # Last AI is narrative (no JSON), budget fine → trigger
        state = {"messages": [HumanMessage(content="q"), AIMessage(content="narrative conclusion")]}
        forced_msg = AIMessage(content='{"primary_category":"logic","confidence":0.8}')
        mw = ForcedFinalCallMiddleware()
        with (
            patch("src.llm_factory.get_llm_for_role") as mock_llm_role,
            patch(
                "src.engine.middleware.forced_call._forced_final_json_call",
                new=AsyncMock(return_value=forced_msg),
            ) as mock_forced,
        ):
            result = await mw.aafter_agent(state=state, runtime=_make_runtime(run_ctx))
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
        with (
            patch("src.llm_factory.get_llm_for_role"),
            patch(
                "src.engine.middleware.forced_call._forced_final_json_call",
                new=AsyncMock(return_value=forced_msg),
            ) as mock_forced,
        ):
            await mw.aafter_agent(state=state, runtime=_make_runtime(run_ctx))
        forced_kwargs = mock_forced.call_args.kwargs
        assert forced_kwargs["natural_stop"] is False

    async def test_forced_call_returns_none_falls_through(
        self, run_ctx: DiagnosisRunContext
    ) -> None:
        """If _forced_final_json_call itself fails (returns None), middleware
        returns None so the node's existing fallback report path runs."""
        state = {"messages": [AIMessage(content="narrative")]}
        mw = ForcedFinalCallMiddleware()
        with (
            patch("src.llm_factory.get_llm_for_role"),
            patch(
                "src.engine.middleware.forced_call._forced_final_json_call",
                new=AsyncMock(return_value=None),
            ),
        ):
            result = await mw.aafter_agent(state=state, runtime=_make_runtime(run_ctx))
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
        with (
            patch("src.llm_factory.get_llm_for_role"),
            patch(
                "src.engine.middleware.forced_call._forced_final_json_call",
                new=AsyncMock(return_value=forced_msg),
            ) as mock_forced,
        ):
            await mw.aafter_agent(state=state, runtime=_make_runtime(run_ctx))
        forced_kwargs = mock_forced.call_args.kwargs
        assert forced_kwargs["invoke_config"].get("callbacks") == [lf]
        assert forced_kwargs["langfuse_handler"] is lf


# ═════════════════════════════════════════════════════════════════════
# Diagnosis node: Langfuse trace lifecycle (start_trace owned by node)
# ═════════════════════════════════════════════════════════════════════


_NODE_RESPONSE = (
    '{"primary_category": "logic", "categories": ["logic"], '
    '"symptom_tier": "backend", "root_cause_tier": "backend", '
    '"root_cause": "x", "affected_file": "a.py", "affected_function": "f", '
    '"fix_suggestion": "fix", "evidence_chain": ["sig-1"], '
    '"confidence": 0.85, "referenced_case_ids": []}'
)


class _RecordingAgent:
    """Replaces the inner create_agent; returns a converged JSON report."""

    async def ainvoke(
        self, state: dict[str, Any], config: Any = None, context: Any = None
    ) -> dict[str, Any]:
        return {"messages": [AIMessage(content=_NODE_RESPONSE)]}


class TestDiagnosisNodeLangfuseTrace:
    """The node owns the Langfuse trace lifecycle (start_trace/end_trace), not
    LangfuseTracingMiddleware -- the handler is a shared singleton, so
    per-diagnosis trace_id/session_id must be set on it explicitly at the node.
    These tests cover the start_trace + set_external_session_id contract that
    previously lived in the middleware (and its graceful-degradation path)."""

    @staticmethod
    def _evidence() -> Any:
        from src.engine.state import NormalizedEvidence, Signal

        return NormalizedEvidence(
            user_report="创建任务后页面卡死",
            golden_signals=[
                Signal(signal_type="error_log", service_tier="backend", summary="TypeError")
            ],
            trigger_time="2026-07-18T10:00:00Z",
            trigger_trace_ids=["self-trace-1"],
        )

    @staticmethod
    def _state(**overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "evidence": TestDiagnosisNodeLangfuseTrace._evidence(),
            "case_id": "test-case-1",
            "findings": [],
            "human_guidance": None,
            "hitl_resumed": False,
        }
        base.update(overrides)
        return base

    @staticmethod
    def _patch_node(monkeypatch: pytest.MonkeyPatch, handler: Any) -> _RecordingAgent:
        from src.engine.nodes import diagnosis_agent as diag_mod

        agent = _RecordingAgent()
        monkeypatch.setattr(diag_mod, "get_diagnosis_agent", lambda: agent)
        monkeypatch.setattr(
            diag_mod, "_get_langfuse_handler_for_dict_state", lambda *a, **k: handler
        )

        async def fake_search(ev: Any, k_final: int = 3, *, now: Any = None) -> list[Any]:
            return []

        monkeypatch.setattr(diag_mod, "search_historical_cases", fake_search)
        return agent

    async def test_node_starts_trace_with_external_trace_id_and_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typing import cast

        from src.engine.nodes import diagnosis_agent as diag_mod
        from src.engine.state import DoctorState

        handler = MagicMock()
        self._patch_node(monkeypatch, handler)

        await diag_mod._diagnosis_agent_node(
            cast(
                DoctorState,
                self._state(langfuse_trace_id="trace-abc", langfuse_session_id="sess-xyz"),
            )
        )

        handler.prepare_for_managed_trace.assert_called_once()
        handler.set_external_session_id.assert_called_once_with("sess-xyz")
        handler.start_trace.assert_called_once()
        _, kwargs = handler.start_trace.call_args
        assert kwargs["trace_id"] == "trace-abc"
        assert "evidence" in kwargs["input_data"]

    async def test_node_langfuse_outage_does_not_block_diagnosis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """start_trace raising must be suppressed (graceful degradation) --
        diagnosis still produces a report."""
        from typing import cast

        from src.engine.nodes import diagnosis_agent as diag_mod
        from src.engine.state import DoctorState

        handler = MagicMock()
        handler.start_trace.side_effect = RuntimeError("langfuse down")
        self._patch_node(monkeypatch, handler)

        result = await diag_mod._diagnosis_agent_node(cast(DoctorState, self._state()))
        assert result["report"] is not None  # diagnosis still produced
