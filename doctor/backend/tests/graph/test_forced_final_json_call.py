"""
Unit tests for the Iteration 1 forced final JSON call mechanism.

Covers the two failure modes observed in baseline (Iteration 0):
- mode 1 (cap + empty content): loop hits MAX_TOOL_CALLS, last AIMessage
  has content="" + tool_calls=[...] → forced call should be made with
  natural_stop=False, and the resulting JSON should be parsed.
- mode 2 (natural stop + narrative): agent natural-stops but emits prose
  without JSON → forced call should be made with natural_stop=True.

Plus regression coverage:
- Healthy case (last AIMessage already has JSON) → forced call NOT triggered.
- Token budget already blown → forced call skipped (would fail anyway).
- Forced call raises / times out → existing fallback path runs.

Helpers tested directly:
- _last_ai_has_json
- _last_ai_is_natural_stop
- _forced_final_json_call (with mocked LLM)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr
from src.graph.nodes.diagnosis_agent import (
    ForcedDiagnosisReport,
    _forced_final_json_call,
    _last_ai_has_json,
    _last_ai_is_natural_stop,
)

from src.engine.state import DoctorState

# ═════════════════════════════════════════════════════════════════════
# _last_ai_has_json
# ═════════════════════════════════════════════════════════════════════


class TestLastAiHasJson:
    def test_returns_true_when_last_ai_has_json_object(self) -> None:
        messages = [
            SystemMessage(content="sys"),
            HumanMessage(content="q"),
            AIMessage(content='{"primary_category":"backend_error","confidence":0.8}'),
        ]
        assert _last_ai_has_json(messages) is True

    def test_returns_true_when_json_in_markdown_fence(self) -> None:
        messages = [
            AIMessage(content='Analysis...\n```json\n{"primary_category":"logic"}\n```'),
        ]
        assert _last_ai_has_json(messages) is True

    def test_returns_false_when_content_is_empty(self) -> None:
        """Failure mode 1: cap + empty content."""
        msg = AIMessage(content="")
        msg.tool_calls = [{"name": "code_search", "args": {}, "id": "1"}]  # type: ignore[attr-defined]
        assert _last_ai_has_json([msg]) is False

    def test_returns_false_when_content_is_narrative_only(self) -> None:
        """Failure mode 2: natural stop + narrative, no JSON."""
        messages = [
            AIMessage(
                content=(
                    "经过调查，根因是 task_service.py 第 42 行的 N+1 查询："
                    "对每个 task 单独查询 comments，导致 20 次额外 DB 调用。"
                    "建议使用 selectinload 预加载。"
                )
            ),
        ]
        assert _last_ai_has_json(messages) is False

    def test_returns_false_when_no_ai_message(self) -> None:
        messages = [SystemMessage(content="sys"), HumanMessage(content="q")]
        assert _last_ai_has_json(messages) is False

    def test_uses_last_ai_message_only(self) -> None:
        """If earlier AIMessage has JSON but last doesn't, return False."""
        messages = [
            AIMessage(content='{"intermediate":"finding"}'),
            AIMessage(content="最终根因是 N+1 查询，建议改用 selectinload。"),
        ]
        assert _last_ai_has_json(messages) is False


# ═════════════════════════════════════════════════════════════════════
# _last_ai_is_natural_stop
# ═════════════════════════════════════════════════════════════════════


class TestLastAiIsNaturalStop:
    def test_true_when_no_tool_calls(self) -> None:
        msg = AIMessage(content="narrative conclusion")
        assert _last_ai_is_natural_stop([msg]) is True

    def test_false_when_tool_calls_present(self) -> None:
        """Failure mode 1: cap hit, last message has tool_calls."""
        msg = AIMessage(content="")
        msg.tool_calls = [{"name": "code_search", "args": {}, "id": "1"}]  # type: ignore[attr-defined]
        assert _last_ai_is_natural_stop([msg]) is False

    def test_false_when_no_ai_message(self) -> None:
        messages = [HumanMessage(content="q")]
        assert _last_ai_is_natural_stop(messages) is False


# ═════════════════════════════════════════════════════════════════════
# _forced_final_json_call
# ═════════════════════════════════════════════════════════════════════


def _make_mock_llm(parsed_report: ForcedDiagnosisReport | None) -> tuple[MagicMock, AsyncMock]:
    """Build a mock BaseChatModel whose ``with_structured_output`` returns a
    structured_llm mock whose ``ainvoke`` returns ``{"parsed": parsed_report, ...}``.

    Mirrors the Iteration 2 call path: ``llm.with_structured_output(schema,
    include_raw=True).ainvoke(...)`` returns a dict with a ``parsed`` key.
    Pass ``parsed_report=None`` to simulate the model emitting no matching
    tool_call (parsed-None branch).
    """
    mock_llm = MagicMock(spec=BaseChatModel)
    structured_llm = MagicMock()
    structured_llm.ainvoke = AsyncMock(
        return_value={"parsed": parsed_report, "raw": AIMessage(content="raw")}
    )
    mock_llm.with_structured_output = MagicMock(return_value=structured_llm)
    return mock_llm, structured_llm.ainvoke


def _sample_report(**overrides: Any) -> ForcedDiagnosisReport:
    """Build a populated ForcedDiagnosisReport for tests."""
    defaults: dict[str, Any] = {
        "primary_category": "backend_error",
        "categories": ["backend_error"],
        "symptom_tier": "backend",
        "root_cause_tier": "backend",
        "root_cause": "N+1 查询",
        "affected_file": "app/services/task_service.py",
        "affected_line": 42,
        "fix_suggestion": "用 selectinload",
        "evidence_chain": ["sig-be020-slow"],
        "confidence": 0.8,
    }
    defaults.update(overrides)
    return ForcedDiagnosisReport(**defaults)


class TestForcedFinalJsonCall:
    async def test_appends_instruction_and_returns_response(self) -> None:
        """Forced call passes the conversation + a JSON-only instruction to the un-bound LLM."""
        report = _sample_report(primary_category="backend_error")
        mock_llm, mock_structured_ainvoke = _make_mock_llm(report)
        messages = [
            SystemMessage(content="sys"),
            HumanMessage(content="evidence"),
            AIMessage(content=""),
        ]

        response = await _forced_final_json_call(
            messages=messages,
            llm=mock_llm,
            invoke_config={},
            natural_stop=False,
            case_id="BE-020",
        )

        assert response is not None
        # Response content is the JSON-serialized ForcedDiagnosisReport
        assert "backend_error" in str(response.content)
        assert "primary_category" in str(response.content)
        # ainvoke should have been called with a list of messages ending in HumanMessage
        sent_messages = mock_structured_ainvoke.call_args[0][0]
        assert isinstance(sent_messages[-1], HumanMessage)
        assert "JSON" in str(sent_messages[-1].content)
        # with_structured_output should have been called with the schema +
        # method="function_calling" (CRITICAL for DeepSeek — default json_schema
        # response_format is rejected with 400) + include_raw=True
        mock_llm.with_structured_output.assert_called_once()
        call_args = mock_llm.with_structured_output.call_args
        assert call_args[0][0] is ForcedDiagnosisReport
        assert call_args[1].get("method") == "function_calling"
        assert call_args[1].get("include_raw") is True
        # original messages list must not be mutated
        assert len(messages) == 3

    async def test_uses_cap_instruction_when_not_natural_stop(self) -> None:
        """Mode 1 (cap): instruction mentions 'tool call upper limit'."""
        mock_llm, mock_structured_ainvoke = _make_mock_llm(_sample_report())
        messages = [AIMessage(content="")]
        await _forced_final_json_call(
            messages=messages,
            llm=mock_llm,
            invoke_config={},
            natural_stop=False,
            case_id="BE-020",
        )
        sent_messages = mock_structured_ainvoke.call_args[0][0]
        instruction_text = str(sent_messages[-1].content)
        assert "工具调用上限" in instruction_text

    async def test_uses_narrative_instruction_when_natural_stop(self) -> None:
        """Mode 2 (narrative): instruction mentions 'narrative text / format as JSON'."""
        mock_llm, mock_structured_ainvoke = _make_mock_llm(_sample_report())
        messages = [AIMessage(content="narrative conclusion")]
        await _forced_final_json_call(
            messages=messages,
            llm=mock_llm,
            invoke_config={},
            natural_stop=True,
            case_id="BE-021",
        )
        sent_messages = mock_structured_ainvoke.call_args[0][0]
        instruction_text = str(sent_messages[-1].content)
        assert "叙事性文字" in instruction_text

    async def test_returns_none_when_llm_raises(self) -> None:
        """If the forced call itself fails (API error / timeout), return None."""
        mock_llm = MagicMock(spec=BaseChatModel)
        structured_llm = MagicMock()
        structured_llm.ainvoke = AsyncMock(side_effect=RuntimeError("API timeout"))
        mock_llm.with_structured_output = MagicMock(return_value=structured_llm)
        messages = [AIMessage(content="")]

        response = await _forced_final_json_call(
            messages=messages,
            llm=mock_llm,
            invoke_config={},
            natural_stop=False,
            case_id="BE-020",
        )
        assert response is None

    async def test_returns_none_when_model_emits_no_tool_call(self) -> None:
        """If the model emits no matching tool_call (parsed=None), return None."""
        mock_llm, _ = _make_mock_llm(parsed_report=None)
        messages = [AIMessage(content="")]

        response = await _forced_final_json_call(
            messages=messages,
            llm=mock_llm,
            invoke_config={},
            natural_stop=False,
            case_id="BE-020",
        )
        assert response is None

    async def test_response_content_is_valid_json_with_all_fields(self) -> None:
        """Synthesized AIMessage content is valid JSON parseable by json.loads."""
        import json as _json

        report = _sample_report(
            fix_suggestion='表现为"登录成功后马上就掉登录态"',  # the unescaped-quote pattern from CONFIG-020
        )
        mock_llm, _ = _make_mock_llm(report)
        messages = [AIMessage(content="")]

        response = await _forced_final_json_call(
            messages=messages,
            llm=mock_llm,
            invoke_config={},
            natural_stop=False,
            case_id="CONFIG-020",
        )
        assert response is not None
        # model_dump_json produces properly-escaped JSON by construction —
        # the unescaped quotes inside fix_suggestion get escaped to \"...
        data = _json.loads(response.content)
        assert data["primary_category"] == "backend_error"
        assert "登录成功后马上就掉登录态" in data["fix_suggestion"]


# ═════════════════════════════════════════════════════════════════════
# Langfuse observability: record_structured_output integration
# ═════════════════════════════════════════════════════════════════════


class TestStructuredOutputObservability:
    """Verify the parsed structured output is recorded to Langfuse.

    The callback path (``on_llm_end``) captures the raw model response
    (an AIMessage with content="" + tool_calls=[...]) but NOT the parsed
    Pydantic object — LangChain materializes it AFTER the callback fires.
    So ``_forced_final_json_call`` explicitly calls
    ``langfuse_handler.record_structured_output`` to make the Iteration 2
    mechanism's actual output visible end-to-end in Langfuse.
    """

    async def test_records_parsed_report_on_success(self) -> None:
        """On success: record_structured_output called with parsed dict + schema name."""
        report = _sample_report(primary_category="logic")
        mock_llm, _ = _make_mock_llm(report)
        handler = MagicMock()
        messages = [AIMessage(content="")]

        await _forced_final_json_call(
            messages=messages,
            llm=mock_llm,
            invoke_config={},
            natural_stop=False,
            case_id="LOGIC-022",
            langfuse_handler=handler,
        )

        handler.record_structured_output.assert_called_once()
        kwargs = handler.record_structured_output.call_args.kwargs
        assert kwargs["schema_name"] == "ForcedDiagnosisReport"
        assert kwargs["parsed"] is not None
        assert kwargs["parsed"]["primary_category"] == "logic"
        assert kwargs["case_id"] == "LOGIC-022"
        # error must NOT be set on success
        assert "error" not in kwargs

    async def test_records_error_on_parsed_none(self) -> None:
        """On parsed=None: record_structured_output called with parsed=None + error."""
        mock_llm, _ = _make_mock_llm(parsed_report=None)
        handler = MagicMock()
        messages = [AIMessage(content="")]

        await _forced_final_json_call(
            messages=messages,
            llm=mock_llm,
            invoke_config={},
            natural_stop=False,
            case_id="BE-020",
            langfuse_handler=handler,
        )

        handler.record_structured_output.assert_called_once()
        kwargs = handler.record_structured_output.call_args.kwargs
        assert kwargs["schema_name"] == "ForcedDiagnosisReport"
        assert kwargs["parsed"] is None
        assert "error" in kwargs
        assert "no matching tool_call" in kwargs["error"]

    async def test_records_error_on_exception(self) -> None:
        """On LLM exception: record_structured_output called with parsed=None + error."""
        mock_llm = MagicMock(spec=BaseChatModel)
        structured_llm = MagicMock()
        structured_llm.ainvoke = AsyncMock(side_effect=RuntimeError("API timeout"))
        mock_llm.with_structured_output = MagicMock(return_value=structured_llm)
        handler = MagicMock()
        messages = [AIMessage(content="")]

        await _forced_final_json_call(
            messages=messages,
            llm=mock_llm,
            invoke_config={},
            natural_stop=False,
            case_id="BE-020",
            langfuse_handler=handler,
        )

        handler.record_structured_output.assert_called_once()
        kwargs = handler.record_structured_output.call_args.kwargs
        assert kwargs["schema_name"] == "ForcedDiagnosisReport"
        assert kwargs["parsed"] is None
        assert "RuntimeError" in kwargs["error"]
        assert "API timeout" in kwargs["error"]

    async def test_no_handler_no_error(self) -> None:
        """Default langfuse_handler=None must not raise (graceful no-op)."""
        report = _sample_report()
        mock_llm, _ = _make_mock_llm(report)
        messages = [AIMessage(content="")]

        # No langfuse_handler passed — must not raise AttributeError.
        response = await _forced_final_json_call(
            messages=messages,
            llm=mock_llm,
            invoke_config={},
            natural_stop=False,
            case_id="BE-020",
        )
        assert response is not None

    async def test_handler_record_exception_swallowed(self) -> None:
        """If record_structured_output itself raises, the forced call must still succeed.

        Observability code must never break the diagnosis path — the
        contextlib.suppress(Exception) guard around the handler call
        ensures a Langfuse outage / bug doesn't propagate.
        """
        report = _sample_report()
        mock_llm, _ = _make_mock_llm(report)
        handler = MagicMock()
        handler.record_structured_output.side_effect = RuntimeError("langfuse down")
        messages = [AIMessage(content="")]

        # Must NOT raise — the handler exception is suppressed.
        response = await _forced_final_json_call(
            messages=messages,
            llm=mock_llm,
            invoke_config={},
            natural_stop=False,
            case_id="BE-020",
            langfuse_handler=handler,
        )
        assert response is not None
        assert "backend_error" in str(response.content)
        # The handler was still called (the suppress is around the call, not
        # preventing it).
        handler.record_structured_output.assert_called_once()


# ═════════════════════════════════════════════════════════════════════
# Integration: forced call wired into diagnosis_agent_node
# ═════════════════════════════════════════════════════════════════════


@pytest.fixture
def be020_state() -> DoctorState:
    from src.engine.state import (
        Correlation,
        NormalizedEvidence,
        Signal,
    )

    evidence = NormalizedEvidence(
        user_report="任务列表页面加载很慢",
        golden_signals=[
            Signal(
                signal_id="sig-be020-slow",
                source="trace",
                signal_type="repeated_query",
                service_tier="backend",
                severity="error",
                summary="N+1 detected: SELECT comments repeated 20 times",
            ),
        ],
        correlations=[
            Correlation(
                correlation_id="corr-be020",
                trace_id="be020abc123",
                description="N+1",
                backend_signals=["sig-be020-slow"],
                confidence=0.92,
            ),
        ],
        frontend_span_count=3,
        backend_span_count=25,
    )
    return DoctorState(
        evidence=evidence,
        case_id="BE-020",
    )


class _ScriptedChatModel(BaseChatModel):
    """Fake chat model that returns scripted AIMessages for create_agent tests.

    Implements the full Runnable interface (via BaseChatModel) so create_agent's
    internal model node can drive the real ReAct loop without real API calls.
    ``bind_tools`` returns self (the loop doesn't actually need tools bound for
    the scripted responses). ``with_structured_output`` returns a
    ``_StructuredScriptedLLM`` whose ``ainvoke`` returns the forced-call shaped
    dict ``{"parsed": ..., "raw": ...}`` that ``_forced_final_json_call`` expects.
    """

    responses: list[AIMessage]
    forced_result: dict[str, Any] | None = None
    _idx: int = PrivateAttr(default=0)
    with_structured_output_call_count: int = 0

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> _ScriptedChatModel:  # type: ignore[override]
        return self

    def with_structured_output(self, schema: Any, **kwargs: Any) -> _StructuredScriptedLLM:
        self.with_structured_output_call_count += 1
        return _StructuredScriptedLLM(forced_result=self.forced_result)

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:  # type: ignore[override]
        if self._idx >= len(self.responses):
            # Run out of scripted responses — return a natural stop with JSON
            # so the loop terminates cleanly rather than raising.
            resp = AIMessage(content='{"primary_category":"performance","confidence":0.5}')
        else:
            resp = self.responses[self._idx]
            self._idx += 1
        return ChatResult(generations=[ChatGeneration(message=resp)])

    async def _agenerate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:  # type: ignore[override]
        return self._generate(messages, stop, run_manager, **kwargs)

    @property
    def _llm_type(self) -> str:
        return "scripted-test"


class _StructuredScriptedLLM:
    """Stand-in for the Runnable returned by with_structured_output."""

    def __init__(self, forced_result: dict[str, Any] | None) -> None:
        self.forced_result = forced_result
        self.await_count = 0

    async def ainvoke(self, messages: Any, config: Any = None, **kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
        self.await_count += 1
        return self.forced_result or {"parsed": None, "raw": AIMessage(content="")}


@pytest.fixture
def _fake_echo_tool(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace get_all_tools with a single instant fake tool for tests.

    Avoids real search_observability/code_search network calls during the
    end-to-end node test. The fake tool returns instantly so the create_agent
    ToolNode doesn't hang.
    """
    from langchain_core.tools import tool

    @tool
    def echo_tool(text: str) -> str:
        """Echo text back (test stand-in for diagnostic tools)."""
        return f"echoed: {text}"

    import src.graph.subgraphs.diagnosis_agent as subgraph_mod

    monkeypatch.setattr(subgraph_mod, "get_all_tools", lambda: [echo_tool])
    return echo_tool


@pytest.fixture
def _clear_agent_cache() -> Any:
    """Clear the cached create_agent so it rebuilds with the mocked LLM/tools."""
    from src.engine.agent import clear_diagnosis_agent_cache

    clear_diagnosis_agent_cache()
    yield
    clear_diagnosis_agent_cache()


class TestForcedCallWiredIntoNode:
    """End-to-end: verify the forced call is triggered / skipped correctly.

    Drives the REAL create_agent + 5 middlewares with a _ScriptedChatModel
    (BaseChatModel subclass) so the actual ReAct loop runs and the
    ForcedFinalCallMiddleware.aafter_agent branch is exercised. A fake echo
    tool replaces the real diagnostic tools to avoid network calls.
    """

    async def test_forced_call_triggers_on_cap_with_empty_content(
        self,
        be020_state: DoctorState,
        monkeypatch: pytest.MonkeyPatch,
        _fake_echo_tool: Any,
        _clear_agent_cache: Any,
    ) -> None:
        """Mode 1: every loop iteration returns tool_calls → BudgetGuard caps at
        MAX_TOOL_CALLS → ForcedFinalCallMiddleware.aafter_agent runs
        with_structured_output and its parsed ForcedDiagnosisReport is appended
        as a JSON AIMessage that parse_diagnosis_report picks up.
        """
        from src.graph.nodes.diagnosis_agent import node as node_module

        # Scripted loop responses: every call returns a tool_call so the loop
        # runs until BudgetGuard caps at MAX_TOOL_CALLS. Each response gets a
        # unique tool_call id so the model_to_tools edge routes to the tools
        # node every iteration (matching the hand-written loop's range(12)
        # semantics); ToolDedupMiddleware still short-circuits the actual tool
        # execution after the 1st (same name+args), so only 1 real echo runs.
        # ``type: "tool_call"`` is required by ToolNode._parse_input to take the
        # "tool_calls" input branch (create_agent normalizes real model output
        # to include it; the scripted bypass needs it explicitly).
        def _tc_msg(i: int) -> AIMessage:
            msg = AIMessage(content="")
            msg.tool_calls = [  # type: ignore[attr-defined]
                {
                    "name": "echo_tool",
                    "args": {"text": "x"},
                    "id": f"t{i}",
                    "type": "tool_call",
                },
            ]
            return msg

        parsed_report = ForcedDiagnosisReport(
            primary_category="performance",
            categories=["performance"],
            symptom_tier="frontend",
            root_cause_tier="backend",
            root_cause="N+1 查询",
            affected_file="app/services/task_service.py",
            affected_line=42,
            fix_suggestion="用 selectinload",
            evidence_chain=["sig-be020-slow"],
            confidence=0.85,
        )
        # MAX_TOOL_CALLS=12 loop responses (all tool_calls → cap), then forced
        structured = {"parsed": parsed_report, "raw": AIMessage(content="raw")}
        mock_llm = _ScriptedChatModel(
            responses=[_tc_msg(i) for i in range(15)],  # extra in case recursion differs
            forced_result=structured,
        )

        import src.graph.subgraphs.diagnosis_agent as subgraph_mod

        import src.llm_factory as llm_factory_mod

        monkeypatch.setattr(llm_factory_mod, "get_llm_for_role", lambda _role: mock_llm)
        # The subgraph bound `get_llm_for_role` at module import time
        # (`from src.llm_factory import get_llm_for_role`), so _get_llm() calls
        # the subgraph's own reference — patch THAT too, or the loop LLM stays
        # real while only the ForcedFinalCall middleware (which imports
        # `src.llm_factory as _llm_factory`) gets the mock.
        monkeypatch.setattr(subgraph_mod, "get_llm_for_role", lambda _role: mock_llm)
        # Disable real Langfuse.
        import src.observability.langfuse_tracing as lf_mod

        monkeypatch.setattr(
            lf_mod, "get_langfuse_handler", MagicMock(side_effect=ImportError("off"))
        )

        result = await node_module.diagnosis_agent_node(be020_state)

        report = result["report"]
        assert report.primary_category == "performance"
        assert report.affected_file == "app/services/task_service.py"
        # Forced call path (with_structured_output) must have been invoked.
        assert mock_llm.with_structured_output_call_count >= 1

    async def test_forced_call_skipped_when_last_ai_already_has_json(
        self,
        be020_state: DoctorState,
        monkeypatch: pytest.MonkeyPatch,
        _fake_echo_tool: Any,
        _clear_agent_cache: Any,
    ) -> None:
        """Healthy case: agent natural-stops with a JSON report on the first
        call → ForcedFinalCallMiddleware gate (``_last_ai_has_json``) skips →
        with_structured_output is NEVER called.
        """
        from src.graph.nodes.diagnosis_agent import node as node_module

        json_msg = AIMessage(
            content=(
                '{"primary_category":"performance","categories":["performance"],'
                '"symptom_tier":"frontend","root_cause_tier":"backend",'
                '"root_cause":"N+1 查询","affected_file":"app/services/task_service.py",'
                '"affected_line":42,"fix_suggestion":"用 selectinload",'
                '"evidence_chain":["sig-be020-slow"],"confidence":0.92}'
            )
        )
        mock_llm = _ScriptedChatModel(
            responses=[json_msg],  # natural stop with JSON on first call
            forced_result={"parsed": None, "raw": AIMessage(content="")},
        )

        import src.graph.subgraphs.diagnosis_agent as subgraph_mod

        import src.llm_factory as llm_factory_mod

        monkeypatch.setattr(llm_factory_mod, "get_llm_for_role", lambda _role: mock_llm)
        monkeypatch.setattr(subgraph_mod, "get_llm_for_role", lambda _role: mock_llm)
        import src.observability.langfuse_tracing as lf_mod

        monkeypatch.setattr(
            lf_mod, "get_langfuse_handler", MagicMock(side_effect=ImportError("off"))
        )

        result = await node_module.diagnosis_agent_node(be020_state)

        report = result["report"]
        assert report.primary_category == "performance"
        assert report.confidence == 0.92
        # Forced call (with_structured_output) must NOT have been called.
        assert mock_llm.with_structured_output_call_count == 0
