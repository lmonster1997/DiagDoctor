"""Unit tests for TriageAgent node v2 (src/graph/nodes/triage.py)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.graph.nodes.triage import (
    _format_correlations,
    _format_golden_signals,
    _format_similar_cases,
    route_after_triage,
    triage_node,
)
from src.graph.state import (
    VALID_CATEGORIES,
    CategoryScore,
    Correlation,
    DoctorState,
    Finding,
    LogEntry,
    NormalizedEvidence,
    Signal,
    TraceSpan,
    TriageOutput,
)

# ── Fixtures ────────────────────────────────────────────────────────


def _make_log(level: str, message: str, service: str = "api") -> LogEntry:
    """Helper to create a LogEntry quickly."""
    return LogEntry(
        timestamp=datetime(2026, 6, 23, 12, 0, 0, tzinfo=UTC),
        level=level,
        service=service,
        service_name=service,
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
        service_name=service,
        start=datetime(2026, 6, 23, 12, 0, 0, tzinfo=UTC),
        duration_ms=duration_ms,
        status=status,  # type: ignore[arg-type]
    )


@pytest.fixture
def empty_evidence() -> NormalizedEvidence:
    """NormalizedEvidence with no signals or correlations."""
    return NormalizedEvidence(user_report="Something is broken")


@pytest.fixture
def populated_evidence() -> NormalizedEvidence:
    """NormalizedEvidence with signals and correlations."""
    return NormalizedEvidence(
        user_report="任务列表打开非常慢，每次都等很久",
        golden_signals=[
            Signal(
                signal_id="sig-01",
                source="log",
                service_tier="backend",
                severity="error",
                summary="Slow query detected: 2.3s",
            ),
            Signal(
                signal_id="sig-02",
                source="trace",
                service_tier="backend",
                severity="warning",
                summary="Slow span: db_query (2300.0ms)",
            ),
        ],
        correlations=[
            Correlation(
                correlation_id="corr-01",
                trace_id="abc123",
                description="前端(1条) → 后端(2条) → DB(1条)",
                confidence=0.8,
            ),
        ],
        frontend_span_count=1,
        backend_span_count=3,
        noise_ratio=0.3,
    )


@pytest.fixture
def minimal_state(populated_evidence: NormalizedEvidence) -> DoctorState:
    """Minimal DoctorState for testing."""
    return DoctorState(evidence=populated_evidence)


# ── Helper function tests ───────────────────────────────────────────


class TestFormatGoldenSignals:
    """Tests for _format_golden_signals helper."""

    def test_empty_signals(self) -> None:
        """Should return placeholder for empty signals."""
        evidence = NormalizedEvidence(user_report="test")
        result = _format_golden_signals(evidence)
        assert "无关键信号" in result

    def test_formats_signals_correctly(self) -> None:
        """Should format signal with tier and severity markers."""
        evidence = NormalizedEvidence(
            golden_signals=[
                Signal(
                    signal_id="sig-01",
                    source="log",
                    service_tier="frontend",
                    severity="error",
                    summary="TypeError in TaskBoard",
                ),
            ]
        )
        result = _format_golden_signals(evidence)
        assert "TaskBoard" in result


class TestFormatCorrelations:
    """Tests for _format_correlations helper."""

    def test_empty_correlations(self) -> None:
        """Should return placeholder for empty correlations."""
        evidence = NormalizedEvidence(user_report="test")
        result = _format_correlations(evidence)
        assert "无跨层关联" in result

    def test_formats_correlations_correctly(self) -> None:
        """Should format correlation with trace_id and confidence."""
        evidence = NormalizedEvidence(
            correlations=[
                Correlation(
                    correlation_id="corr-01",
                    trace_id="abc123",
                    description="frontend→backend",
                    confidence=0.9,
                ),
            ]
        )
        result = _format_correlations(evidence)
        assert "corr-01" in result
        assert "abc123" in result


class TestFormatSimilarCases:
    """Tests for _format_similar_cases helper."""

    def test_empty_cases(self) -> None:
        """Should return placeholder for empty cases."""
        result = _format_similar_cases([])
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
        ]
        result = _format_similar_cases(cases)
        assert "0.92" in result
        assert "performance" in result
        assert "N+1 query issue" in result


class TestRouteAfterTriage:
    """Tests for route_after_triage gate function."""

    def test_low_confidence_fallback(self) -> None:
        """Low confidence should route to general_agent."""
        state = DoctorState(
            triage=TriageOutput(
                scores=[CategoryScore(category="backend_error", confidence=0.3)],
                primary="backend_error",
            )
        )
        targets = route_after_triage(state)
        assert targets == ["general_agent"]

    def test_high_confidence_single_specialist(self) -> None:
        """High confidence single should route to one specialist."""
        state = DoctorState(
            triage=TriageOutput(
                scores=[CategoryScore(category="frontend_crash", confidence=0.85)],
                primary="frontend_crash",
            )
        )
        targets = route_after_triage(state)
        assert "frontend_specialist" in targets

    def test_cross_layer_fan_out(self) -> None:
        """Cross-layer suspicion should fan out two specialists."""
        state = DoctorState(
            triage=TriageOutput(
                scores=[
                    CategoryScore(category="frontend_crash", confidence=0.85),
                    CategoryScore(category="backend_error", confidence=0.65),
                ],
                primary="frontend_crash",
                cross_layer_suspected=True,
            )
        )
        targets = route_after_triage(state)
        assert "frontend_specialist" in targets
        assert "backend_specialist" in targets

    def test_empty_scores_fallback(self) -> None:
        """Empty scores should fallback to general_agent."""
        state = DoctorState(triage=TriageOutput(scores=[], primary=""))
        targets = route_after_triage(state)
        assert targets == ["general_agent"]


# ── triage_node integration tests ───────────────────────────────────


class TestTriageNode:
    """Tests for the triage_node async function (v2)."""

    @pytest.mark.asyncio
    async def test_returns_valid_triage(self, minimal_state: DoctorState) -> None:
        """Should return a TriageOutput with valid categories."""
        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value.ainvoke = AsyncMock(
            return_value={
                "scores": [{"category": "performance", "confidence": 0.85}],
                "primary": "performance",
                "reasoning": "慢查询证据明确",
                "cross_layer_suspected": False,
            }
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

        assert "triage" in result
        triage_output = result["triage"]
        assert triage_output.primary in VALID_CATEGORIES

    @pytest.mark.asyncio
    async def test_returns_finding_with_correct_fields(
        self, minimal_state: DoctorState
    ) -> None:
        """Should return findings list with required fields."""
        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value.ainvoke = AsyncMock(
            return_value={
                "scores": [{"category": "performance", "confidence": 0.9}],
                "primary": "performance",
                "reasoning": "慢查询",
                "cross_layer_suspected": False,
            }
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
        mock_llm.with_structured_output.return_value.ainvoke = AsyncMock(
            side_effect=ValueError("structured output not supported")
        )
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

        assert "triage" in result
        assert result["triage"].primary in VALID_CATEGORIES
        assert result["findings"][0].confidence <= 0.5

    @pytest.mark.asyncio
    async def test_integrates_rag_similar_cases(
        self, minimal_state: DoctorState
    ) -> None:
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
            return_value={
                "scores": [{"category": "performance", "confidence": 0.88}],
                "primary": "performance",
                "reasoning": "匹配历史案例",
                "cross_layer_suspected": False,
            }
        )

        with (
            patch("src.graph.nodes.triage.ChatOpenAI", return_value=mock_llm),
            patch(
                "src.graph.nodes.triage.get_knowledge_service",
                return_value=mock_knowledge,
            ),
        ):
            result = await triage_node(minimal_state)

        mock_knowledge.search_historical_cases.assert_awaited_once()
        assert "triage" in result

    @pytest.mark.asyncio
    async def test_normalizes_invalid_category(self, minimal_state: DoctorState) -> None:
        """Should normalize invalid category to backend_error."""
        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value.ainvoke = AsyncMock(
            return_value={
                "scores": [{"category": "some_unknown_category", "confidence": 0.7}],
                "primary": "some_unknown_category",
                "reasoning": "unknown",
                "cross_layer_suspected": False,
            }
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

        assert result["triage"].primary == "backend_error"


class TestTriageOutputModel:
    """Tests for TriageOutput Pydantic model (v2 multi-label)."""

    def test_valid_multi_label_output(self) -> None:
        """Should accept multi-label scores with primary."""
        output = TriageOutput(
            scores=[
                CategoryScore(category="frontend_crash", confidence=0.9),
                CategoryScore(category="backend_error", confidence=0.4),
            ],
            primary="frontend_crash",
            reasoning="JS error on page load, possible backend API issue",
            cross_layer_suspected=True,
        )
        assert output.primary == "frontend_crash"
        assert len(output.scores) == 2
        assert output.cross_layer_suspected is True

    def test_default_values(self) -> None:
        """Should use sensible defaults."""
        output = TriageOutput()
        assert output.primary == ""
        assert output.scores == []
        assert output.cross_layer_suspected is False


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
