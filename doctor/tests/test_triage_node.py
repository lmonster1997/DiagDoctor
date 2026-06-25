"""Unit tests for TriageAgent node (src/graph/nodes/triage.py)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.graph.nodes.triage import (
    VALID_CATEGORIES,
    TriageOutput,
    format_similar_cases,
    summarize_logs,
    summarize_traces,
    triage_node,
)
from src.graph.state import DoctorState, Evidence, Finding, LogEntry, TraceSpan

# ── Fixtures ────────────────────────────────────────────────────────


def _make_log(level: str, message: str, service: str = "api") -> LogEntry:
    """Helper to create a LogEntry quickly."""
    return LogEntry(
        timestamp=datetime(2026, 6, 23, 12, 0, 0, tzinfo=UTC),
        level=level,
        service=service,
        message=message,
    )


def _make_span(
    name: str,
    duration_ms: float,
    status: str = "ok",
    service: str = "api",
) -> TraceSpan:
    """Helper to create a TraceSpan quickly."""
    return TraceSpan(
        span_id=f"span-{name}",
        name=name,
        service=service,
        start=datetime(2026, 6, 23, 12, 0, 0, tzinfo=UTC),
        duration_ms=duration_ms,
        status=status,  # type: ignore[arg-type]
    )


@pytest.fixture
def empty_evidence() -> Evidence:
    """Evidence with no logs/traces."""
    return Evidence(user_report="Something is broken")


@pytest.fixture
def populated_evidence() -> Evidence:
    """Evidence with logs and traces."""
    return Evidence(
        user_report="任务列表打开非常慢，每次都等很久",
        logs=[
            _make_log("INFO", "Request started GET /api/tasks"),
            _make_log("ERROR", "Task not found: id=999"),
            _make_log("WARNING", "Slow query detected: 2.3s"),
            _make_log("INFO", "Response sent 200"),
        ],
        traces=[
            _make_span("GET /api/tasks", 2500.0, status="error"),
            _make_span("db_query", 2300.0, status="ok"),
            _make_span("get_task", 50.0, status="ok"),
            _make_span("get_assignee", 180.0, status="ok"),
        ],
    )


@pytest.fixture
def minimal_state(populated_evidence: Evidence) -> DoctorState:
    """Minimal DoctorState for testing."""
    return DoctorState(evidence=populated_evidence)


# ── Helper function tests ───────────────────────────────────────────


class TestSummarizeLogs:
    """Tests for summarize_logs helper."""

    def test_empty_logs(self) -> None:
        """Should return placeholder for empty logs."""
        result = summarize_logs([])
        assert "无日志数据" in result

    def test_prioritizes_errors(self) -> None:
        """ERROR logs should appear before INFO logs."""
        logs = [
            _make_log("INFO", "info msg"),
            _make_log("ERROR", "error msg"),
            _make_log("INFO", "another info"),
        ]
        result = summarize_logs(logs)
        # ERROR should appear before INFO
        error_pos = result.index("ERROR")
        info_pos = result.index("INFO")
        assert error_pos < info_pos

    def test_truncates_to_max_entries(self) -> None:
        """Should respect max_entries limit."""
        logs = [_make_log("INFO", f"msg {i}") for i in range(100)]
        result = summarize_logs(logs, max_entries=10)
        assert len(result.split("\n")) <= 10


class TestSummarizeTraces:
    """Tests for summarize_traces helper."""

    def test_empty_traces(self) -> None:
        """Should return placeholder for empty traces."""
        result = summarize_traces([])
        assert "无 Trace 数据" in result

    def test_prioritizes_errors(self) -> None:
        """Error spans should be listed first."""
        traces = [
            _make_span("fast_ok", 10.0),
            _make_span("error_span", 100.0, status="error"),
            _make_span("slow_span", 500.0),
        ]
        result = summarize_traces(traces)
        # error should appear before slow
        error_pos = result.index("error_span")
        slow_pos = result.index("slow_span")
        assert error_pos < slow_pos

    def test_marks_slow_spans(self) -> None:
        """Spans exceeding the threshold should be marked."""
        traces = [
            _make_span("slow_one", 300.0),
            _make_span("fast_one", 50.0),
        ]
        result = summarize_traces(traces, slow_threshold_ms=200.0)
        assert "slow_one" in result
        assert "fast_one" in result
        # Slow span should appear before fast span
        assert result.index("slow_one") < result.index("fast_one")


class TestFormatSimilarCases:
    """Tests for format_similar_cases helper."""

    def test_empty_cases(self) -> None:
        """Should return placeholder for empty cases."""
        result = format_similar_cases([])
        assert "无类似历史案例" in result

    def test_formats_cases_correctly(self) -> None:
        """Should format cases with similarity, category, and root cause."""
        cases = [
            {
                "case_id": "case-001",
                "category": "performance",
                "root_cause": "N+1 query issue",
                "similarity_score": 0.92,
            },
            {
                "case_id": "case-002",
                "category": "backend_error",
                "root_cause": "Unhandled exception in auth",
                "similarity_score": 0.75,
            },
        ]
        result = format_similar_cases(cases)
        assert "0.92" in result
        assert "performance" in result
        assert "N+1 query issue" in result
        assert "0.75" in result
        assert "backend_error" in result


# ── triage_node integration tests ───────────────────────────────────


class TestTriageNode:
    """Tests for the triage_node async function."""

    @pytest.mark.asyncio
    async def test_returns_valid_category(self, minimal_state: DoctorState) -> None:
        """Should return a valid bug category from VALID_CATEGORIES."""
        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value.ainvoke = AsyncMock(
            return_value=TriageOutput(
                category="performance",
                confidence=0.85,
                reasoning="查询慢，日志显示慢查询警告，Trace 显示 DB 调用耗时超过2秒",
            )
        )

        with (
            patch("src.graph.nodes.triage.ChatOpenAI", return_value=mock_llm),
            patch(
                "src.graph.nodes.triage.get_knowledge_service",
                return_value=MagicMock(
                    search_historical_cases=AsyncMock(return_value=[]),
                ),
            ),
        ):
            result = await triage_node(minimal_state)

        assert "bug_category" in result
        assert result["bug_category"] in VALID_CATEGORIES

    @pytest.mark.asyncio
    async def test_returns_finding_with_correct_fields(self, minimal_state: DoctorState) -> None:
        """Should return findings list with required fields."""
        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value.ainvoke = AsyncMock(
            return_value=TriageOutput(
                category="performance",
                confidence=0.9,
                reasoning="日志显示慢查询，SPAN 耗时超阈值",
            )
        )

        with (
            patch("src.graph.nodes.triage.ChatOpenAI", return_value=mock_llm),
            patch(
                "src.graph.nodes.triage.get_knowledge_service",
                return_value=MagicMock(
                    search_historical_cases=AsyncMock(return_value=[]),
                ),
            ),
        ):
            result = await triage_node(minimal_state)

        findings = result.get("findings", [])
        assert len(findings) >= 1
        finding = findings[0]
        assert isinstance(finding, Finding)
        assert finding.agent == "TriageAgent"
        assert finding.summary
        assert 0.0 <= finding.confidence <= 1.0

    @pytest.mark.asyncio
    async def test_falls_back_on_structured_output_failure(
        self, minimal_state: DoctorState
    ) -> None:
        """Should gracefully fallback when structured output fails."""
        mock_llm = MagicMock()
        # Structured output raises an exception
        mock_llm.with_structured_output.return_value.ainvoke = AsyncMock(
            side_effect=ValueError("structured output not supported")
        )
        # Fallback unstructured call succeeds
        mock_llm.ainvoke = AsyncMock(
            return_value=MagicMock(content="这是性能问题，category: performance")
        )

        with (
            patch("src.graph.nodes.triage.ChatOpenAI", return_value=mock_llm),
            patch(
                "src.graph.nodes.triage.get_knowledge_service",
                return_value=MagicMock(
                    search_historical_cases=AsyncMock(return_value=[]),
                ),
            ),
        ):
            result = await triage_node(minimal_state)

        assert "bug_category" in result
        assert result["bug_category"] in VALID_CATEGORIES
        assert result["findings"][0].confidence <= 0.5  # fallback uses 0.5

    @pytest.mark.asyncio
    async def test_integrates_rag_similar_cases(self, minimal_state: DoctorState) -> None:
        """Should call knowledge service and include similar cases."""
        mock_knowledge = MagicMock()
        mock_knowledge.search_historical_cases = AsyncMock(
            return_value=[
                {
                    "case_id": "case-001",
                    "category": "performance",
                    "root_cause": "N+1 query in task listing",
                    "similarity_score": 0.92,
                },
            ]
        )

        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value.ainvoke = AsyncMock(
            return_value=TriageOutput(
                category="performance",
                confidence=0.88,
                reasoning="匹配历史案例 case-001",
            )
        )

        with (
            patch("src.graph.nodes.triage.ChatOpenAI", return_value=mock_llm),
            patch(
                "src.graph.nodes.triage.get_knowledge_service",
                return_value=mock_knowledge,
            ),
        ):
            await triage_node(minimal_state)

        # Verify RAG was called
        mock_knowledge.search_historical_cases.assert_called_once()

    @pytest.mark.asyncio
    async def test_normalizes_invalid_category(self, minimal_state: DoctorState) -> None:
        """Should normalize invalid category to backend_error."""
        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value.ainvoke = AsyncMock(
            return_value=TriageOutput(
                category="some_unknown_category",
                confidence=0.7,
                reasoning="unknown",
            )
        )

        with (
            patch("src.graph.nodes.triage.ChatOpenAI", return_value=mock_llm),
            patch(
                "src.graph.nodes.triage.get_knowledge_service",
                return_value=MagicMock(
                    search_historical_cases=AsyncMock(return_value=[]),
                ),
            ),
        ):
            result = await triage_node(minimal_state)

        assert result["bug_category"] == "backend_error"


class TestTriageOutput:
    """Tests for TriageOutput Pydantic model."""

    def test_valid_output(self) -> None:
        """Should accept valid category and confidence."""
        output = TriageOutput(
            category="frontend_crash",
            confidence=0.9,
            reasoning="JS error on page load",
        )
        assert output.category == "frontend_crash"
        assert output.confidence == 0.9

    def test_confidence_clamped(self) -> None:
        """Should reject confidence outside [0, 1]."""
        with pytest.raises((ValueError, TypeError, AssertionError)):
            TriageOutput(category="data", confidence=1.5, reasoning="test")

    def test_default_values(self) -> None:
        """Should use sensible defaults."""
        output = TriageOutput(category="logic")
        assert output.confidence == 0.7
        assert output.reasoning == ""


class TestValidCategories:
    """Tests for VALID_CATEGORIES constant."""

    def test_contains_all_expected_categories(self) -> None:
        """Should contain all six bug categories."""
        expected = {
            "frontend_crash",
            "backend_error",
            "performance",
            "logic",
            "data",
            "config",
        }
        assert expected == VALID_CATEGORIES

    @pytest.mark.parametrize(
        "category",
        ["frontend_crash", "backend_error", "performance", "logic", "data", "config"],
    )
    def test_each_category_is_valid(self, category: str) -> None:
        """Each expected category should be in VALID_CATEGORIES."""
        assert category in VALID_CATEGORIES
