"""
Unit tests for Backend Specialist subgraph & node wrapper.

Covers:
- ``format_normalized_evidence()`` — evidence formatting for agent prompt
- ``parse_agent_output_to_finding()`` — parsing agent output into Finding
- ``backend_specialist_node()`` — node integration (mocked agent)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src.graph.nodes.backend_specialist import (
    _format_correlations,
    _format_signals,
    backend_specialist_node,
    format_normalized_evidence,
)
from src.graph.state import (
    Correlation,
    DoctorState,
    Finding,
    NormalizedEvidence,
    Signal,
    TriageOutput,
)
from src.graph.subgraphs.backend_specialist import (
    _ensure_list,
    _extract_json_from_text,
    parse_agent_output_to_finding,
    reset_backend_specialist,
)

# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_cache() -> None:
    """Reset the cached specialist agent before each test."""
    reset_backend_specialist()


@pytest.fixture
def empty_state() -> DoctorState:
    """DoctorState with empty evidence — agent should short-circuit."""
    return DoctorState(
        evidence=NormalizedEvidence(),
        triage=TriageOutput(primary="backend_error"),
    )


@pytest.fixture
def populated_state() -> DoctorState:
    """DoctorState with realistic normalized evidence."""
    evidence = NormalizedEvidence(
        user_report="任务列表加载非常慢，每次打开都要等很久",
        golden_signals=[
            Signal(
                signal_id="sig-backend-001",
                source="trace",
                signal_type="repeated_query",
                service_tier="backend",
                severity="error",
                summary="N+1 detected: SELECT comments repeated 30 times for task list",
            ),
            Signal(
                signal_id="sig-backend-002",
                source="trace",
                signal_type="slow_span",
                service_tier="backend",
                severity="warning",
                summary="Slow span: GET /api/projects/{id}/tasks took 3200ms",
            ),
        ],
        correlations=[
            Correlation(
                correlation_id="corr-001",
                trace_id="abc123def456",
                description="Frontend fetch /api/tasks → backend N+1 SELECT comments ×30",
                backend_signals=["sig-backend-001", "sig-backend-002"],
                confidence=0.9,
            ),
        ],
        frontend_span_count=5,
        backend_span_count=35,
        noise_ratio=0.15,
    )
    return DoctorState(
        evidence=evidence,
        triage=TriageOutput(
            primary="performance",
            cross_layer_suspected=False,
        ),
    )


@pytest.fixture
def cross_layer_state() -> DoctorState:
    """State for a cross-layer bug (frontend crash with backend root cause)."""
    evidence = NormalizedEvidence(
        user_report="进入任务看板后页面白屏",
        golden_signals=[
            Signal(
                signal_id="sig-fe-001",
                source="browser_error",
                signal_type="error_log",
                service_tier="frontend",
                severity="error",
                summary="TypeError: Cannot read properties of undefined (reading 'tags')",
            ),
            Signal(
                signal_id="sig-be-001",
                source="trace",
                signal_type="behavior_mismatch",
                service_tier="backend",
                severity="warning",
                summary="GET /api/tasks response missing 'tags' field in TaskResponse schema",
            ),
        ],
        correlations=[
            Correlation(
                correlation_id="corr-cross-001",
                trace_id="xyz789",
                description=(
                    "Frontend TypeError reading task.tags "
                    "→ backend TaskResponse lacks tags field"
                ),
                frontend_signals=["sig-fe-001"],
                backend_signals=["sig-be-001"],
                confidence=0.85,
            ),
        ],
        frontend_span_count=8,
        backend_span_count=3,
        noise_ratio=0.1,
    )
    return DoctorState(
        evidence=evidence,
        triage=TriageOutput(
            primary="frontend_crash",
            cross_layer_suspected=True,
        ),
    )


# ── format_normalized_evidence tests ──────────────────────────────────


class TestFormatNormalizedEvidence:
    """Tests for evidence formatting into agent prompt."""

    def test_formats_empty_evidence(self) -> None:
        """Empty evidence produces a usable prompt with fallback text."""
        evidence = NormalizedEvidence()
        result = format_normalized_evidence(evidence)

        assert "无关键信号" in result
        assert "无跨层关联数据" in result
        assert "query_loki_logs" in result  # instruction mentions tools

    def test_formats_golden_signals(self, populated_state: DoctorState) -> None:
        """Golden signals are formatted compactly with tier labels."""
        evidence = populated_state.evidence
        result = format_normalized_evidence(evidence)

        assert "sig-backend-001" in result
        assert "N+1" in result
        assert "后端" in result

    def test_formats_correlations(self, populated_state: DoctorState) -> None:
        """Correlations include trace_id and descriptions."""
        evidence = populated_state.evidence
        result = format_normalized_evidence(evidence)

        assert "abc123def456" in result
        assert "corr-001" in result
        assert "confidence=0.9" in result

    def test_formats_cross_layer_evidence(self, cross_layer_state: DoctorState) -> None:
        """Cross-layer evidence includes both frontend and backend signals."""
        evidence = cross_layer_state.evidence
        result = format_normalized_evidence(evidence)

        assert "sig-fe-001" in result
        assert "sig-be-001" in result
        assert "前端" in result  # frontend signal has tier label

    def test_includes_context_summary(self, populated_state: DoctorState) -> None:
        """Evidence context includes span counts and noise ratio."""
        evidence = populated_state.evidence
        result = format_normalized_evidence(evidence)

        assert "前端 span 数：5" in result
        assert "后端 span 数：35" in result
        assert "15%" in result

    def test_includes_user_report(self, populated_state: DoctorState) -> None:
        """User report is included at the top."""
        evidence = populated_state.evidence
        result = format_normalized_evidence(evidence)

        assert "任务列表加载非常慢" in result


# ── _format_signals tests ────────────────────────────────────────────


class TestFormatSignals:
    """Tests for the _format_signals helper."""

    def test_empty_signals(self) -> None:
        """Empty signal list returns placeholder."""
        result = _format_signals([])
        assert "无信号" in result

    def test_formats_backend_error_signal(self) -> None:
        """Backend error signal shows correct tier and severity."""
        signals = [
            Signal(
                signal_id="sig-01",
                source="log",
                signal_type="error_log",
                service_tier="backend",
                severity="error",
                summary="500 Internal Server Error in /api/tasks",
            ),
        ]
        result = _format_signals(signals)
        assert "❌" in result
        assert "后端" in result
        assert "sig-01" in result
        assert "500 Internal" in result

    def test_formats_frontend_warning_signal(self) -> None:
        """Frontend warning signal shows correct tier and severity."""
        signals = [
            Signal(
                signal_id="sig-fe-01",
                source="browser_error",
                signal_type="error_log",
                service_tier="frontend",
                severity="warning",
                summary="Console warning: deprecated API",
            ),
        ]
        result = _format_signals(signals)
        assert "⚠️" in result
        assert "前端" in result

    def test_caps_at_30_signals(self) -> None:
        """Only first 30 signals are formatted."""
        signals = [
            Signal(
                signal_id=f"sig-{i:03d}",
                source="log",
                service_tier="backend",
                severity="info",
                summary=f"Signal {i}",
            )
            for i in range(50)
        ]
        result = _format_signals(signals)
        # Should have 30 lines of signals, not 50
        lines = [
            line
            for line in result.split("\n")
            if line.strip().startswith(("❌", "⚠️", "ℹ️"))
        ]
        assert len(lines) <= 30


# ── _format_correlations tests ────────────────────────────────────────


class TestFormatCorrelations:
    """Tests for the _format_correlations helper."""

    def test_empty_correlations(self) -> None:
        """Empty correlation list returns placeholder."""
        result = _format_correlations([])
        assert "无关联" in result

    def test_formats_correlation_with_trace_id(self) -> None:
        """Correlation includes trace_id and confidence."""
        correlations = [
            Correlation(
                correlation_id="corr-01",
                trace_id="trace123",
                description="Frontend error linked to backend 500",
                confidence=0.92,
            ),
        ]
        result = _format_correlations(correlations)
        assert "trace123" in result
        assert "corr-01" in result
        assert "0.9" in result

    def test_caps_at_10_correlations(self) -> None:
        """Only first 10 correlations are formatted."""
        correlations = [
            Correlation(correlation_id=f"corr-{i}", description=f"Correlation {i}")
            for i in range(20)
        ]
        result = _format_correlations(correlations)
        # Should have at most 10 lines, not 20
        lines = [
            line for line in result.split("\n") if line.strip().startswith("  -")
        ]
        assert len(lines) <= 10


# ── parse_agent_output_to_finding tests ───────────────────────────────


class TestParseAgentOutputToFinding:
    """Tests for parsing ReAct agent output into Finding."""

    def test_parses_valid_json_in_markdown_fence(self) -> None:
        """JSON in a markdown code fence is extracted correctly."""
        agent_result = {
            "messages": [
                HumanMessage(content="diagnose this"),
                AIMessage(
                    content="""
Here is my analysis:

```json
{
  "summary": "N+1 query in list_tasks",
  "affected_files": ["app/services/task_service.py"],
  "fix_suggestion": "Add selectinload(comments)",
  "evidence_refs": ["sig-backend-001"],
  "confidence": 0.9,
  "contradiction": false,
  "cross_layer": false
}
```
"""
                ),
            ]
        }
        finding = parse_agent_output_to_finding(agent_result)

        assert finding.agent == "backend_specialist"
        assert finding.summary == "N+1 query in list_tasks"
        assert finding.affected_files == ["app/services/task_service.py"]
        assert finding.fix_suggestion == "Add selectinload(comments)"
        assert finding.evidence_refs == ["sig-backend-001"]
        assert finding.confidence == 0.9
        assert finding.contradiction is False
        assert finding.cross_layer is False

    def test_parses_raw_json_object(self) -> None:
        """Raw JSON object (no code fence) is extracted."""
        agent_result = {
            "messages": [
                AIMessage(
                    content=(
                        '{"summary":"Missing index on tasks table",'
                        '"affected_files":["app/models/task.py"],'
                        '"fix_suggestion":"CREATE INDEX idx_tasks_status ON tasks(status)",'
                        '"evidence_refs":["sig-001"],'
                        '"confidence":0.75,"contradiction":false,"cross_layer":false}'
                    ),
                ),
            ]
        }
        finding = parse_agent_output_to_finding(agent_result)

        assert finding.summary == "Missing index on tasks table"
        assert finding.confidence == 0.75

    def test_parses_contradiction_flag(self) -> None:
        """Contradiction=True is preserved."""
        agent_result = {
            "messages": [
                AIMessage(
                    content=(
                        "```json\n"
                        '{"summary":"Evidence contradicts classification",'
                        '"affected_files":[],'
                        '"fix_suggestion":"Re-triage needed",'
                        '"evidence_refs":[],"confidence":0.3,'
                        '"contradiction":true,"cross_layer":true}\n'
                        "```"
                    ),
                ),
            ]
        }
        finding = parse_agent_output_to_finding(agent_result)
        assert finding.contradiction is True
        assert finding.cross_layer is True

    def test_falls_back_to_raw_text(self) -> None:
        """When no JSON is found, uses the raw AI message as summary."""
        agent_result = {
            "messages": [
                AIMessage(
                    content=(
                        "I analyzed the logs and found a database connection "
                        "timeout. The root cause appears to be..."
                    ),
                ),
            ]
        }
        finding = parse_agent_output_to_finding(agent_result)

        assert "database connection timeout" in finding.summary
        assert finding.confidence == 0.3  # fallback confidence
        assert finding.evidence_refs == []

    def test_handles_missing_messages(self) -> None:
        """Agent result with no messages returns a placeholder Finding."""
        finding = parse_agent_output_to_finding({"messages": []})

        assert "未返回有效诊断" in finding.summary
        assert finding.confidence == 0.0

    def test_uses_last_ai_message_only(self) -> None:
        """Only the last AI message is used for parsing."""
        agent_result = {
            "messages": [
                AIMessage(content="intermediate reasoning"),
                AIMessage(
                    content=(
                        "```json\n"
                        '{"summary":"Final conclusion",'
                        '"affected_files":["app/main.py"],'
                        '"fix_suggestion":"Fix import",'
                        '"evidence_refs":[],"confidence":0.99,'
                        '"contradiction":false,"cross_layer":false}\n'
                        "```"
                    ),
                ),
            ]
        }
        finding = parse_agent_output_to_finding(agent_result)
        assert finding.summary == "Final conclusion"
        assert finding.confidence == 0.99


# ── _extract_json_from_text tests ─────────────────────────────────────


class TestExtractJsonFromText:
    """Tests for JSON extraction from agent text output."""

    def test_extracts_from_markdown_json_fence(self) -> None:
        result = _extract_json_from_text('```json\n{"key": "value"}\n```')
        assert result == {"key": "value"}

    def test_extracts_from_markdown_no_lang_fence(self) -> None:
        result = _extract_json_from_text('```\n{"key": "value"}\n```')
        assert result == {"key": "value"}

    def test_extracts_raw_braced_json(self) -> None:
        result = _extract_json_from_text('Here is {"key": "value"} in text')
        assert result == {"key": "value"}

    def test_returns_none_for_no_json(self) -> None:
        result = _extract_json_from_text("No JSON here at all")
        assert result is None

    def test_extracts_nested_json(self) -> None:
        text = """```json
{
  "summary": "test",
  "evidence_refs": ["a", "b"],
  "nested": {"x": 1}
}
```"""
        result = _extract_json_from_text(text)
        assert result is not None
        assert result["summary"] == "test"
        assert result["evidence_refs"] == ["a", "b"]


# ── _ensure_list tests ───────────────────────────────────────────────


class TestEnsureList:
    """Tests for the _ensure_list helper."""

    def test_passes_through_list(self) -> None:
        assert _ensure_list(["a", "b"]) == ["a", "b"]

    def test_wraps_string(self) -> None:
        assert _ensure_list("hello") == ["hello"]

    def test_empty_string_returns_empty(self) -> None:
        assert _ensure_list("") == []

    def test_converts_non_string_items(self) -> None:
        assert _ensure_list([1, 2, 3]) == ["1", "2", "3"]


# ── backend_specialist_node integration tests ─────────────────────────


class TestBackendSpecialistNode:
    """Tests for the node function (mocked agent).

    Note: ``backend_specialist_node`` imports ``get_backend_specialist``
    inside the function body (lazy import pattern). Therefore we must
    patch the source module: ``src.graph.subgraphs.backend_specialist``.
    """

    async def test_skips_when_no_evidence(self, empty_state: DoctorState) -> None:
        """Node returns a placeholder Finding when evidence is empty."""
        result = await backend_specialist_node(empty_state)

        findings: list[Finding] = result["findings"]  # type: ignore[arg-type]
        assert len(findings) == 1
        assert "证据不足" in findings[0].summary
        assert findings[0].confidence == 0.0

    @patch(
        "src.graph.subgraphs.backend_specialist.get_backend_specialist",
        autospec=True,
    )
    async def test_invokes_agent_with_formatted_evidence(
        self, mock_get_agent: MagicMock, populated_state: DoctorState
    ) -> None:
        """Agent is invoked with formatted evidence as HumanMessage."""
        mock_agent = AsyncMock()
        mock_agent.ainvoke = AsyncMock(
            return_value={
                "messages": [
                    AIMessage(
                        content='```json\n{"summary":"N+1","affected_files":["a.py"],'
                        '"fix_suggestion":"Fix","evidence_refs":["s1"],"confidence":0.9,'
                        '"contradiction":false,"cross_layer":false}\n```'
                    )
                ]
            }
        )
        mock_get_agent.return_value = mock_agent

        result = await backend_specialist_node(populated_state)

        # Verify agent was called
        mock_agent.ainvoke.assert_called_once()
        call_args = mock_agent.ainvoke.call_args[0][0]

        # Verify input contains formatted evidence
        messages = call_args["messages"]
        assert len(messages) == 1
        assert isinstance(messages[0], HumanMessage)
        assert "sig-backend-001" in str(messages[0].content)
        assert "N+1" in str(messages[0].content)

        # Verify Finding is parsed correctly
        findings: list[Finding] = result["findings"]  # type: ignore[arg-type]
        assert len(findings) == 1
        assert findings[0].summary == "N+1"
        assert findings[0].confidence == 0.9

    @patch(
        "src.graph.subgraphs.backend_specialist.get_backend_specialist",
        autospec=True,
    )
    async def test_handles_agent_error(
        self, mock_get_agent: MagicMock, populated_state: DoctorState
    ) -> None:
        """Node returns error Finding when agent fails."""
        mock_agent = AsyncMock()
        mock_agent.ainvoke = AsyncMock(side_effect=RuntimeError("LLM timeout"))
        mock_get_agent.return_value = mock_agent

        result = await backend_specialist_node(populated_state)

        findings: list[Finding] = result["findings"]  # type: ignore[arg-type]
        assert len(findings) == 1
        assert "LLM timeout" in findings[0].summary
        assert findings[0].confidence == 0.0
