"""
Budget exceeded tests for UnifiedAgent (V3).

Covers:
- Budget exceeded by tool call count (>12)
- Budget exceeded by token count (>100k)
- Budget exceeded by elapsed time (>300s)
- early_stopped propagation through unified_agent_node
- Best-effort report generation when budget exceeded

Uses mocked agents — no real LLM calls required.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from src.graph.nodes.unified_agent import (
    BUDGET_WARNING_THRESHOLD,
    MAX_TIME_SECONDS,
    MAX_TOKENS_BUDGET,
    MAX_TOOL_CALLS,
    is_budget_exceeded,
    unified_agent_node,
    update_budget,
)
from src.graph.state import (
    BudgetState,
    Correlation,
    DiagnosisReport,
    DoctorState,
    NormalizedEvidence,
    Signal,
    TriageOutput,
)
from src.graph.subgraphs.unified_agent import clear_unified_agent_cache

# ═════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def reset_cache() -> None:
    """Reset the cached UnifiedAgent before each test."""
    clear_unified_agent_cache()


@pytest.fixture
def base_state() -> DoctorState:
    """Base state with minimal evidence for budget tests."""
    evidence = NormalizedEvidence(
        user_report="应用响应缓慢",
        golden_signals=[
            Signal(
                signal_id="sig-001",
                source="trace",
                signal_type="slow_span",
                service_tier="backend",
                severity="warning",
                summary="Slow DB query 5000ms",
            ),
        ],
        correlations=[
            Correlation(
                correlation_id="corr-001",
                trace_id="tracebudget123",
                description="Frontend slow -> backend slow query",
                backend_signals=["sig-001"],
                confidence=0.8,
            ),
        ],
        frontend_span_count=1,
        backend_span_count=5,
        noise_ratio=0.05,
    )
    return DoctorState(
        evidence=evidence,
        triage=TriageOutput(primary="performance"),
        case_id="BUDGET-TEST",
    )


# ═════════════════════════════════════════════════════════════════════
# Budget threshold constants tests
# ═════════════════════════════════════════════════════════════════════


class TestBudgetConstants:
    """Verify budget constants are set to handbook values."""

    def test_max_tool_calls_is_12(self) -> None:
        assert MAX_TOOL_CALLS == 12

    def test_budget_warning_is_8(self) -> None:
        """Warning threshold at 8 tool calls (best-effort trigger)."""
        assert BUDGET_WARNING_THRESHOLD == 8

    def test_max_tokens_is_100k(self) -> None:
        assert MAX_TOKENS_BUDGET == 100_000

    def test_max_time_is_300s(self) -> None:
        assert MAX_TIME_SECONDS == 300


# ═════════════════════════════════════════════════════════════════════
# is_budget_exceeded edge case tests
# ═════════════════════════════════════════════════════════════════════


class TestIsBudgetExceededEdgeCases:
    """Edge cases for the is_budget_exceeded function."""

    def test_exactly_at_limit_tool_calls(self) -> None:
        """Exactly 12 tool calls IS exceeded."""
        budget = BudgetState(tool_calls=12, total_tokens=0, elapsed_seconds=0)
        assert is_budget_exceeded(budget) is True

    def test_just_below_limit_tool_calls(self) -> None:
        """11 tool calls is NOT exceeded."""
        budget = BudgetState(tool_calls=11, total_tokens=0, elapsed_seconds=0)
        assert is_budget_exceeded(budget) is False

    def test_exactly_at_limit_tokens(self) -> None:
        """Exactly 100k tokens IS exceeded."""
        budget = BudgetState(tool_calls=0, total_tokens=100_000, elapsed_seconds=0)
        assert is_budget_exceeded(budget) is True

    def test_just_below_limit_tokens(self) -> None:
        """99,999 tokens is NOT exceeded."""
        budget = BudgetState(tool_calls=0, total_tokens=99_999, elapsed_seconds=0)
        assert is_budget_exceeded(budget) is False

    def test_exactly_at_time_limit(self) -> None:
        """Exactly 300s IS exceeded."""
        budget = BudgetState(tool_calls=0, total_tokens=0, elapsed_seconds=300.0)
        assert is_budget_exceeded(budget) is True

    def test_just_below_time_limit(self) -> None:
        """299.9s is NOT exceeded."""
        budget = BudgetState(tool_calls=0, total_tokens=0, elapsed_seconds=299.9)
        assert is_budget_exceeded(budget) is False

    def test_all_three_exceeded(self) -> None:
        """All three limits exceeded simultaneously."""
        budget = BudgetState(tool_calls=15, total_tokens=200_000, elapsed_seconds=400.0)
        assert is_budget_exceeded(budget) is True


# ═════════════════════════════════════════════════════════════════════
# update_budget edge case tests
# ═════════════════════════════════════════════════════════════════════


class TestUpdateBudgetEdgeCases:
    """Edge cases for the update_budget function."""

    def test_preserves_started_at(self) -> None:
        """update_budget preserves the original started_at timestamp."""
        original_time = datetime(2026, 6, 28, 10, 0, 0, tzinfo=UTC)
        budget = BudgetState(started_at=original_time)

        updated = update_budget(budget, {"messages": []})
        assert updated.started_at == original_time

    def test_sets_started_at_when_none(self) -> None:
        """update_budget sets started_at if it was None."""
        budget = BudgetState(started_at=None)

        updated = update_budget(budget, {"messages": []})
        assert updated.started_at is not None

    def test_accumulates_tool_calls(self) -> None:
        """Multiple updates accumulate tool call counts."""
        budget = BudgetState(tool_calls=3)

        msg = AIMessage(content="")
        msg.tool_calls = [{"name": "tool_a", "args": {}}, {"name": "tool_b", "args": {}}]  # type: ignore[attr-defined]

        updated = update_budget(budget, {"messages": [msg]})
        assert updated.tool_calls == 5  # 3 + 2

    def test_handles_messages_without_tool_calls(self) -> None:
        """Messages without tool_calls attribute don't increment count."""
        budget = BudgetState(tool_calls=0)
        updated = update_budget(budget, {"messages": [AIMessage(content="no tools")]})
        assert updated.tool_calls == 0

    def test_elapsed_time_is_calculated(self) -> None:
        """elapsed_seconds is computed from started_at."""
        started = datetime(2026, 6, 28, 10, 0, 0, tzinfo=UTC)
        budget = BudgetState(started_at=started)

        updated = update_budget(budget, {"messages": []})
        # elapsed should be > 0 (time has passed since started_at)
        assert updated.elapsed_seconds >= 0.0


# ═════════════════════════════════════════════════════════════════════
# Node-level budget exceeded tests (mocked agent)
# ═════════════════════════════════════════════════════════════════════


class TestNodeBudgetExceeded:
    """Tests that unified_agent_node correctly handles budget exceeded."""

    @patch(
        "src.graph.subgraphs.unified_agent.get_unified_agent",
        autospec=True,
    )
    async def test_early_stopped_when_tool_calls_exceeded(
        self, mock_get_agent: MagicMock, base_state: DoctorState
    ) -> None:
        """
        When agent accumulates >12 tool calls, early_stopped=True.

        The node pre-sets budget to 10 calls, agent makes 3 more → total 13 > 12.
        """
        base_state.budget = BudgetState(
            tool_calls=10,
            total_tokens=0,
            started_at=datetime.now(UTC),
        )

        mock_agent = AsyncMock()
        msg = AIMessage(
            content='{"primary_category":"performance","root_cause":"N+1","confidence":0.6}'
        )
        msg.tool_calls = [  # type: ignore[attr-defined]
            {"name": "search_observability", "args": {}},
            {"name": "code_search", "args": {}},
            {"name": "db_query", "args": {}},
        ]
        mock_agent.ainvoke = AsyncMock(return_value={"messages": [msg]})
        mock_get_agent.return_value = mock_agent

        result = await unified_agent_node(base_state)

        assert result["early_stopped"] is True
        report = result["report"]
        assert isinstance(report, DiagnosisReport)
        assert report.early_stopped is True
        # Budget note should be set
        assert "预算超限" in report.notes or report.notes != ""

    @patch(
        "src.graph.subgraphs.unified_agent.get_unified_agent",
        autospec=True,
    )
    async def test_budget_not_exceeded_normal_operation(
        self, mock_get_agent: MagicMock, base_state: DoctorState
    ) -> None:
        """
        Normal operation (few tool calls) does NOT trigger early_stopped.
        """
        base_state.budget = BudgetState(
            tool_calls=0,
            total_tokens=0,
            started_at=datetime.now(UTC),
        )

        mock_agent = AsyncMock()
        msg = AIMessage(
            content="""
{
  "primary_category": "performance",
  "categories": ["performance"],
  "symptom_tier": "frontend",
  "root_cause_tier": "backend",
  "root_cause": "Slow DB query due to missing index",
  "affected_file": "app/models/task.py",
  "affected_line": 15,
  "fix_suggestion": "Add index on status column",
  "evidence_chain": ["sig-001"],
  "confidence": 0.85
}
"""
        )
        msg.tool_calls = [  # type: ignore[attr-defined]
            {"name": "search_observability", "args": {}},
        ]
        mock_agent.ainvoke = AsyncMock(return_value={"messages": [msg]})
        mock_get_agent.return_value = mock_agent

        result = await unified_agent_node(base_state)

        assert result["early_stopped"] is False
        report = result["report"]
        assert report is not None
        assert report.early_stopped is False
        assert report.confidence == 0.85

    @patch(
        "src.graph.subgraphs.unified_agent.get_unified_agent",
        autospec=True,
    )
    async def test_budget_at_warning_threshold_not_exceeded(
        self, mock_get_agent: MagicMock, base_state: DoctorState
    ) -> None:
        """
        8 tool calls (warning threshold) is NOT exceeded — still allows best-effort.

        The node should continue and produce a report, but confidence may be lower.
        """
        base_state.budget = BudgetState(
            tool_calls=6,  # agent makes 2 more → total 8
            total_tokens=0,
            started_at=datetime.now(UTC),
        )

        mock_agent = AsyncMock()
        msg = AIMessage(
            content="""
{
  "primary_category": "performance",
  "categories": ["performance"],
  "symptom_tier": "frontend",
  "root_cause_tier": "backend",
  "root_cause": "Probable N+1 in list_tasks",
  "affected_file": "app/services/task_service.py",
  "affected_line": 42,
  "fix_suggestion": "Use selectinload",
  "evidence_chain": ["sig-001"],
  "confidence": 0.65
}
"""
        )
        msg.tool_calls = [  # type: ignore[attr-defined]
            {"name": "search_observability", "args": {}},
            {"name": "code_search", "args": {}},
        ]
        mock_agent.ainvoke = AsyncMock(return_value={"messages": [msg]})
        mock_get_agent.return_value = mock_agent

        result = await unified_agent_node(base_state)

        # 8 calls = warning, not yet exceeded
        assert result["early_stopped"] is False
        report = result["report"]
        assert report is not None
        assert report.early_stopped is False

    @patch(
        "src.graph.subgraphs.unified_agent.get_unified_agent",
        autospec=True,
    )
    async def test_failure_handler_sets_early_stopped(
        self, mock_get_agent: MagicMock, base_state: DoctorState
    ) -> None:
        """
        When agent raises exception, early_stopped=True via handle_agent_failure.
        """
        mock_agent = AsyncMock()
        mock_agent.ainvoke = AsyncMock(side_effect=RuntimeError("Budget exhausted: token limit"))
        mock_get_agent.return_value = mock_agent

        result = await unified_agent_node(base_state)

        assert result["early_stopped"] is True
        report = result["report"]
        assert "Budget exhausted" in report.root_cause

    @patch(
        "src.graph.subgraphs.unified_agent.get_unified_agent",
        autospec=True,
    )
    async def test_very_long_operation_triggers_time_budget(
        self, mock_get_agent: MagicMock, base_state: DoctorState
    ) -> None:
        """
        When elapsed time exceeds 300s, early_stopped is set.

        This simulates a diagnosis that started 301s ago.
        """
        started_long_ago = datetime(2026, 6, 28, 9, 54, 0, tzinfo=UTC)  # ~6 min ago
        base_state.budget = BudgetState(
            tool_calls=0,
            total_tokens=0,
            started_at=started_long_ago,
        )

        mock_agent = AsyncMock()
        msg = AIMessage(
            content='{"primary_category":"performance","root_cause":"slow","confidence":0.5}'
        )
        mock_agent.ainvoke = AsyncMock(return_value={"messages": [msg]})
        mock_get_agent.return_value = mock_agent

        result = await unified_agent_node(base_state)

        # elapsed should exceed 300s
        assert result["early_stopped"] is True
