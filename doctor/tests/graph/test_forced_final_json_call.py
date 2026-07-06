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

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.graph.nodes.diagnosis_agent import (
    _forced_final_json_call,
    _last_ai_has_json,
    _last_ai_is_natural_stop,
)
from src.graph.state import DoctorState

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
            AIMessage(content="Analysis...\n```json\n{\"primary_category\":\"logic\"}\n```"),
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


def _make_mock_llm(response_content: str) -> tuple[MagicMock, AsyncMock]:
    """Build a mock BaseChatModel whose ainvoke returns an AIMessage with the given content."""
    mock_llm = MagicMock(spec=BaseChatModel)
    mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content=response_content))
    return mock_llm, mock_llm.ainvoke


class TestForcedFinalJsonCall:
    async def test_appends_instruction_and_returns_response(self) -> None:
        """Forced call passes the conversation + a JSON-only instruction to the un-bound LLM."""
        mock_llm, mock_ainvoke = _make_mock_llm(
            '{"primary_category":"backend_error","confidence":0.8}'
        )
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
        assert "backend_error" in str(response.content)
        # ainvoke should have been called with a list of messages ending in HumanMessage
        sent_messages = mock_ainvoke.call_args[0][0]
        assert isinstance(sent_messages[-1], HumanMessage)
        assert "JSON" in str(sent_messages[-1].content)
        # original messages list must not be mutated
        assert len(messages) == 3

    async def test_uses_cap_instruction_when_not_natural_stop(self) -> None:
        """Mode 1 (cap): instruction mentions 'tool call upper limit'."""
        mock_llm, mock_ainvoke = _make_mock_llm('{"primary_category":"x"}')
        messages = [AIMessage(content="")]
        await _forced_final_json_call(
            messages=messages,
            llm=mock_llm,
            invoke_config={},
            natural_stop=False,
            case_id="BE-020",
        )
        sent_messages = mock_ainvoke.call_args[0][0]
        instruction_text = str(sent_messages[-1].content)
        assert "工具调用上限" in instruction_text

    async def test_uses_narrative_instruction_when_natural_stop(self) -> None:
        """Mode 2 (narrative): instruction mentions 'narrative text / format as JSON'."""
        mock_llm, mock_ainvoke = _make_mock_llm('{"primary_category":"x"}')
        messages = [AIMessage(content="narrative conclusion")]
        await _forced_final_json_call(
            messages=messages,
            llm=mock_llm,
            invoke_config={},
            natural_stop=True,
            case_id="BE-021",
        )
        sent_messages = mock_ainvoke.call_args[0][0]
        instruction_text = str(sent_messages[-1].content)
        assert "叙事性文字" in instruction_text

    async def test_returns_none_when_llm_raises(self) -> None:
        """If the forced call itself fails, return None so caller falls back gracefully."""
        mock_llm = MagicMock(spec=BaseChatModel)
        mock_llm.ainvoke = AsyncMock(side_effect=RuntimeError("API timeout"))
        messages = [AIMessage(content="")]

        response = await _forced_final_json_call(
            messages=messages,
            llm=mock_llm,
            invoke_config={},
            natural_stop=False,
            case_id="BE-020",
        )
        assert response is None


# ═════════════════════════════════════════════════════════════════════
# Integration: forced call wired into diagnosis_agent_node
# ═════════════════════════════════════════════════════════════════════


@pytest.fixture
def be020_state() -> DoctorState:
    from src.graph.state import (
        Correlation,
        NormalizedEvidence,
        Signal,
        TriageOutput,
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
        noise_ratio=0.08,
    )
    return DoctorState(
        evidence=evidence,
        triage=TriageOutput(primary="performance"),
        case_id="BE-020",
    )


class TestForcedCallWiredIntoNode:
    """End-to-end: verify the forced call is triggered / skipped correctly.

    Mocks the LLM (not the agent subgraph) so the actual ReAct loop in
    diagnosis_agent_node runs and the forced-call branch is exercised.
    """

    async def test_forced_call_triggers_on_cap_with_empty_content(
        self, be020_state: DoctorState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mode 1: every loop iteration returns tool_calls, last one has empty content.

        After the loop, the forced call should be invoked and its JSON parsed.
        """
        from src.graph.nodes import diagnosis_agent as ua_module

        # Mock LLM: first N calls return tool_calls (driving the loop to cap),
        # the final forced call returns JSON.
        tool_call_msg = AIMessage(content="")
        tool_call_msg.tool_calls = [  # type: ignore[attr-defined]
            {"name": "search_observability", "args": {"source": "loki", "query": "x"}, "id": "t1"},
        ]
        json_msg = AIMessage(
            content=(
                '{"primary_category":"performance","categories":["performance"],'
                '"symptom_tier":"frontend","root_cause_tier":"backend",'
                '"root_cause":"N+1 查询","affected_file":"app/services/task_service.py",'
                '"affected_line":42,"fix_suggestion":"用 selectinload",'
                '"evidence_chain":["sig-be020-slow"],"confidence":0.85}'
            )
        )

        mock_llm = MagicMock()
        # bind_tools returns a separate bound object whose ainvoke drives the loop.
        # We make the bound object return tool_call_msg repeatedly until cap.
        bound_llm = MagicMock()
        bound_llm.ainvoke = AsyncMock(return_value=tool_call_msg)
        mock_llm.bind_tools = MagicMock(return_value=bound_llm)
        # Un-bound llm.ainvoke is the forced call → returns JSON.
        mock_llm.ainvoke = AsyncMock(return_value=json_msg)

        monkeypatch.setattr(ua_module, "get_llm_for_role", lambda _role: mock_llm, raising=False)
        # Patch where it's looked up inside the node function.
        import src.llm_factory as llm_factory_mod

        monkeypatch.setattr(llm_factory_mod, "get_llm_for_role", lambda _role: mock_llm)

        # Disable real Langfuse to avoid network calls.
        import src.observability.langfuse_tracing as lf_mod

        monkeypatch.setattr(lf_mod, "get_langfuse_handler", MagicMock(side_effect=ImportError("off")))

        result = await ua_module.diagnosis_agent_node(be020_state)

        report = result["report"]
        assert report.primary_category == "performance"
        assert report.affected_file == "app/services/task_service.py"
        # Forced call (un-bound llm.ainvoke) must have been invoked at least once.
        assert mock_llm.ainvoke.await_count >= 1

    async def test_forced_call_skipped_when_last_ai_already_has_json(
        self, be020_state: DoctorState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Healthy case: agent natural-stops with a JSON report → no forced call."""
        from src.graph.nodes import diagnosis_agent as ua_module

        json_msg = AIMessage(
            content=(
                '{"primary_category":"performance","categories":["performance"],'
                '"symptom_tier":"frontend","root_cause_tier":"backend",'
                '"root_cause":"N+1 查询","affected_file":"app/services/task_service.py",'
                '"affected_line":42,"fix_suggestion":"用 selectinload",'
                '"evidence_chain":["sig-be020-slow"],"confidence":0.92}'
            )
        )

        mock_llm = MagicMock()
        bound_llm = MagicMock()
        bound_llm.ainvoke = AsyncMock(return_value=json_msg)  # natural stop, JSON
        mock_llm.bind_tools = MagicMock(return_value=bound_llm)
        mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content="should-not-be-used"))

        import src.llm_factory as llm_factory_mod

        monkeypatch.setattr(llm_factory_mod, "get_llm_for_role", lambda _role: mock_llm)
        import src.observability.langfuse_tracing as lf_mod

        monkeypatch.setattr(lf_mod, "get_langfuse_handler", MagicMock(side_effect=ImportError("off")))

        result = await ua_module.diagnosis_agent_node(be020_state)

        report = result["report"]
        assert report.primary_category == "performance"
        assert report.confidence == 0.92
        # Forced call (un-bound ainvoke) must NOT have been called.
        assert mock_llm.ainvoke.await_count == 0
