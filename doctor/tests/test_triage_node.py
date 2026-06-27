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

    # ── Cross-layer enforcement edge cases (D21 fix) ──────────────

    def test_cross_layer_forces_backend_when_second_is_data(self) -> None:
        """
        FE-020 场景：跨层疑似 True，primary=frontend_crash，第二高是 data(0.55)
        映射到 logic_specialist。cross_layer 修正必须强制补上 backend_specialist。

        修复前 bug：扇出 [frontend_specialist, logic_specialist]，缺 backend_specialist。
        修复后：扇出 [frontend_specialist, logic_specialist, backend_specialist]。
        """
        state = DoctorState(
            triage=TriageOutput(
                scores=[
                    CategoryScore(category="frontend_crash", confidence=0.95),
                    CategoryScore(category="data", confidence=0.55),
                    CategoryScore(category="backend_error", confidence=0.30),
                ],
                primary="frontend_crash",
                reasoning="白屏 + 后端缺字段，data 问题置信度高于 backend_error",
                cross_layer_suspected=True,
            )
        )
        targets = route_after_triage(state)
        assert "frontend_specialist" in targets
        assert "logic_specialist" in targets  # second: data → logic_specialist
        assert "backend_specialist" in targets  # ★ cross-layer 强制补位
        # Order preserved: primary first, then second, then forced
        assert targets[0] == "frontend_specialist"

    def test_cross_layer_forces_frontend_when_primary_is_backend(self) -> None:
        """
        反向跨层：primary=backend_error，cross_layer_suspected=True 但第二高是
        performance（映射到 perf_specialist）。应强制补 frontend_specialist。
        """
        state = DoctorState(
            triage=TriageOutput(
                scores=[
                    CategoryScore(category="backend_error", confidence=0.88),
                    CategoryScore(category="performance", confidence=0.50),
                ],
                primary="backend_error",
                reasoning="后端超时 + 前端感知慢，需排查前端异步调用逻辑",
                cross_layer_suspected=True,
            )
        )
        targets = route_after_triage(state)
        assert "backend_specialist" in targets
        assert "perf_specialist" in targets
        assert "frontend_specialist" in targets  # ★ cross-layer 强制补位

    def test_cross_layer_no_second_but_forces_opposite_tier(self) -> None:
        """
        跨层但只有一个类别（只有 frontend_crash）。第二名为 None 时，
        cross_layer 应直接补 backend_specialist（走 elif 分支）。
        """
        state = DoctorState(
            triage=TriageOutput(
                scores=[CategoryScore(category="frontend_crash", confidence=0.92)],
                primary="frontend_crash",
                reasoning="前端报错，疑似后端返回异常数据",
                cross_layer_suspected=True,
            )
        )
        targets = route_after_triage(state)
        assert targets == ["frontend_specialist", "backend_specialist"]

    def test_no_cross_layer_no_fan_out_single_target(self) -> None:
        """非跨层、无第二高置信度 → 只走单个 specialist。"""
        state = DoctorState(
            triage=TriageOutput(
                scores=[CategoryScore(category="performance", confidence=0.95)],
                primary="performance",
                cross_layer_suspected=False,
            )
        )
        targets = route_after_triage(state)
        assert targets == ["perf_specialist"]

    def test_second_confidence_above_04_triggers_fan_out(self) -> None:
        """第二高置信度 > 0.4 且非跨层 → 扇出两个 specialist。"""
        state = DoctorState(
            triage=TriageOutput(
                scores=[
                    CategoryScore(category="performance", confidence=0.85),
                    CategoryScore(category="backend_error", confidence=0.45),
                ],
                primary="performance",
                cross_layer_suspected=False,
            )
        )
        targets = route_after_triage(state)
        assert "perf_specialist" in targets
        assert "backend_specialist" in targets
        assert len(targets) == 2

    def test_dedup_when_second_maps_same_specialist(self) -> None:
        """第二高类别映射到同一个 specialist 时不应重复。"""
        state = DoctorState(
            triage=TriageOutput(
                scores=[
                    CategoryScore(category="backend_error", confidence=0.90),
                    CategoryScore(category="config", confidence=0.60),
                ],
                primary="backend_error",
                cross_layer_suspected=False,
            )
        )
        targets = route_after_triage(state)
        # config → backend_specialist, same as primary → dedup to 1
        assert targets == ["backend_specialist"]

    def test_exact_boundary_confidence_05(self) -> None:
        """Primary confidence == 0.5 应通过门控（>= 0.5），不走 general_agent。"""
        state = DoctorState(
            triage=TriageOutput(
                scores=[CategoryScore(category="logic", confidence=0.5)],
                primary="logic",
                cross_layer_suspected=False,
            )
        )
        targets = route_after_triage(state)
        assert targets == ["logic_specialist"]
        assert "general_agent" not in targets


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
    async def test_returns_finding_with_correct_fields(self, minimal_state: DoctorState) -> None:
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

    # ── Mock 归一化证据 → 多标签输出（D21 验收核心）──────────────

    @pytest.mark.asyncio
    async def test_multi_label_output_from_frontend_backend_signals(
        self, minimal_state: DoctorState
    ) -> None:
        """
        Mock 归一化证据含前端 client_error + 后端关联信号 →
        验证 triage_node 输出多标签 scores + cross_layer_suspected=True。
        """
        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value.ainvoke = AsyncMock(
            return_value={
                "scores": [
                    {"category": "frontend_crash", "confidence": 0.93},
                    {"category": "backend_error", "confidence": 0.35},
                    {"category": "data", "confidence": 0.55},
                ],
                "primary": "frontend_crash",
                "reasoning": "前端白屏 + 后端 API 缺字段",
                "cross_layer_suspected": True,
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

        triage_output: TriageOutput = result["triage"]
        assert triage_output.cross_layer_suspected is True
        assert len(triage_output.scores) >= 2  # 多标签输出
        # primary 必须是最高置信度
        top_score = max(triage_output.scores, key=lambda s: s.confidence)
        assert triage_output.primary == top_score.category
        # 所有 score 的 category 必须在 VALID_CATEGORIES 中
        for s in triage_output.scores:
            assert s.category in VALID_CATEGORIES

    @pytest.mark.asyncio
    async def test_multi_label_preserves_confidence_sorting(
        self, minimal_state: DoctorState
    ) -> None:
        """
        验证 triage_node 保留 LLM 返回的所有类别 scores（不截断、不重排）。
        """
        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value.ainvoke = AsyncMock(
            return_value={
                "scores": [
                    {"category": "performance", "confidence": 0.95},
                    {"category": "logic", "confidence": 0.50},
                    {"category": "data", "confidence": 0.40},
                ],
                "primary": "performance",
                "reasoning": "3 个潜在类别均有信号",
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

        triage_output: TriageOutput = result["triage"]
        assert len(triage_output.scores) == 3
        # 验证门控：primary=0.95 ≥ 0.5 → 不走 general_agent
        state_after = DoctorState(
            evidence=minimal_state.evidence,
            triage=triage_output,
        )
        targets = route_after_triage(state_after)
        assert "general_agent" not in targets

    @pytest.mark.asyncio
    async def test_triage_node_receives_normalized_signals(
        self, minimal_state: DoctorState
    ) -> None:
        """
        验证 triage_node 使用的是归一化后的 golden_signals + correlations，
        而非原始 raw 数据。
        """
        # 确认 populated_evidence 含 golden_signals 和 correlations
        evidence = minimal_state.evidence
        assert len(evidence.golden_signals) >= 1
        assert len(evidence.correlations) >= 1

        mock_llm = MagicMock()
        mock_llm.with_structured_output.return_value.ainvoke = AsyncMock(
            return_value={
                "scores": [{"category": "performance", "confidence": 0.85}],
                "primary": "performance",
                "reasoning": "慢查询信号明确",
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
        assert result["triage"].primary in VALID_CATEGORIES


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
