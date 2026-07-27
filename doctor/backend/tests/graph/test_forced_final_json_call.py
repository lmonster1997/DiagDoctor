"""
Unit tests for the Iteration 1 forced final JSON call mechanism.

Covers the two failure modes observed in baseline (Iteration 0):
- mode 1 (cap + empty content): loop hits MAX_MODEL_CALLS, last AIMessage
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

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.engine.forced_call import (
    ForcedDiagnosisReport,
    _forced_final_json_call,
    _last_ai_has_json,
    _last_ai_is_natural_stop,
)

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
