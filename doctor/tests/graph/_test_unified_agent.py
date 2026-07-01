"""
Integration tests for UnifiedAgent subgraph & node wrapper (V3).

Covers:
- ``format_evidence_for_agent()`` — evidence formatting for agent HumanMessage
- ``parse_diagnosis_report()`` — parsing agent JSON output into DiagnosisReport
- ``extract_findings()`` — extracting Finding list from agent messages
- ``unified_agent_node()`` — node integration with mocked agent
- Cross-layer diagnosis scenarios (FE-020 style)
- Performance diagnosis scenarios (PERF-020 style)

Uses mock LLM responses — no real LLM calls required.
"""

from __future__ import annotations

from datetime import UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src.graph.nodes.unified_agent import (
    extract_findings,
    format_evidence_for_agent,
    handle_agent_failure,
    is_budget_exceeded,
    parse_diagnosis_report,
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
def empty_state() -> DoctorState:
    """DoctorState with empty evidence — agent should short-circuit."""
    return DoctorState(
        evidence=NormalizedEvidence(),
        triage=TriageOutput(primary=""),
    )


@pytest.fixture
def be020_state() -> DoctorState:
    """
    BE-020 style state: N+1 query performance bug.

    Symptoms: slow task list page load.
    Root cause: list_tasks queries comments one-by-one (N+1 pattern).
    """
    evidence = NormalizedEvidence(
        user_report="任务列表页面加载很慢，点击任务后需要等待很久才能看到详情",
        golden_signals=[
            Signal(
                signal_id="sig-be020-slow",
                source="trace",
                signal_type="repeated_query",
                service_tier="backend",
                severity="error",
                summary="N+1 detected: SELECT comments repeated 20 times in list_tasks",
            ),
            Signal(
                signal_id="sig-be020-trace",
                source="trace",
                signal_type="slow_span",
                service_tier="backend",
                severity="warning",
                summary="Slow span: GET /api/projects/{id}/tasks 4200ms",
            ),
        ],
        correlations=[
            Correlation(
                correlation_id="corr-be020",
                trace_id="be020abc123def456",
                description="Frontend fetch /api/tasks -> backend N+1 SELECT comments x20",
                backend_signals=["sig-be020-slow", "sig-be020-trace"],
                confidence=0.92,
            ),
        ],
        frontend_span_count=3,
        backend_span_count=25,
        noise_ratio=0.08,
    )
    return DoctorState(
        evidence=evidence,
        triage=TriageOutput(
            primary="performance",
            scores=[],
            cross_layer_suspected=False,
        ),
        case_id="BE-020",
    )


@pytest.fixture
def fe020_state() -> DoctorState:
    """
    FE-020 style state: frontend crash with cross-layer root cause.

    Symptoms: page goes blank (TypeError: Cannot read properties of undefined).
    Root cause: backend API response missing 'tags' field.
    """
    evidence = NormalizedEvidence(
        user_report="进入任务看板页面后白屏，控制台报 TypeError",
        golden_signals=[
            Signal(
                signal_id="sig-fe020-crash",
                source="browser_error",
                signal_type="error_log",
                service_tier="frontend",
                severity="error",
                summary="TypeError: Cannot read properties of undefined (reading 'tags')",
            ),
            Signal(
                signal_id="sig-fe020-backend",
                source="trace",
                signal_type="behavior_mismatch",
                service_tier="backend",
                severity="warning",
                summary="GET /api/tasks response missing 'tags' field",
            ),
        ],
        correlations=[
            Correlation(
                correlation_id="corr-fe020-cross",
                trace_id="fe020xyz789abc",
                description="Frontend TypeError reading task.tags -> backend TaskResponse lacks tags field",
                frontend_signals=["sig-fe020-crash"],
                backend_signals=["sig-fe020-backend"],
                confidence=0.88,
            ),
        ],
        frontend_span_count=8,
        backend_span_count=5,
        noise_ratio=0.10,
    )
    return DoctorState(
        evidence=evidence,
        triage=TriageOutput(
            primary="frontend_crash",
            scores=[],
            cross_layer_suspected=True,
        ),
        case_id="FE-020",
    )


@pytest.fixture
def perf020_state() -> DoctorState:
    """
    PERF-020 style state: pure performance bug.

    Symptoms: every page load is slow.
    Root cause: missing DB index causing sequential scan.
    """
    evidence = NormalizedEvidence(
        user_report="整个应用都变慢了，每个页面都要加载 5 秒以上",
        golden_signals=[
            Signal(
                signal_id="sig-perf020-slow",
                source="trace",
                signal_type="slow_span",
                service_tier="backend",
                severity="error",
                summary="Slow DB query: SELECT * FROM tasks WHERE status='pending' took 4800ms",
            ),
            Signal(
                signal_id="sig-perf020-n1",
                source="trace",
                signal_type="repeated_query",
                service_tier="backend",
                severity="warning",
                summary="N+1 query: tasks list followed by N individual queries",
            ),
        ],
        correlations=[
            Correlation(
                correlation_id="corr-perf020",
                trace_id="perf020trace111222",
                description="Frontend page load slow -> backend N+1 + missing index",
                backend_signals=["sig-perf020-slow", "sig-perf020-n1"],
                confidence=0.90,
            ),
        ],
        frontend_span_count=2,
        backend_span_count=30,
        noise_ratio=0.05,
    )
    return DoctorState(
        evidence=evidence,
        triage=TriageOutput(
            primary="performance",
            scores=[],
            cross_layer_suspected=False,
        ),
        case_id="PERF-020",
    )


# ═════════════════════════════════════════════════════════════════════
# format_evidence_for_agent tests
# ═════════════════════════════════════════════════════════════════════


class TestFormatEvidenceForAgent:
    """Tests for evidence formatting into HumanMessage for UnifiedAgent."""

    def test_formats_empty_evidence(self) -> None:
        """Empty evidence produces a usable prompt with fallback text."""
        evidence = NormalizedEvidence()
        result = format_evidence_for_agent(evidence)

        assert "请基于以上实时查询结果进行诊断" in result
        assert "code_search" in result  # instruction mentions tools
        assert "get_file_content" in result

    def test_formats_be020_evidence(self, be020_state: DoctorState) -> None:
        """BE-020 N+1 evidence includes signal IDs and trace_id."""
        evidence = be020_state.evidence
        result = format_evidence_for_agent(evidence)

        assert "sig-be020-slow" in result
        assert "sig-be020-trace" in result
        assert "be020abc123def456" in result
        assert "N+1" in result
        assert "4200ms" in result

    def test_formats_fe020_cross_layer_evidence(self, fe020_state: DoctorState) -> None:
        """FE-020 cross-layer evidence includes frontend AND backend signals."""
        evidence = fe020_state.evidence
        result = format_evidence_for_agent(evidence)

        assert "sig-fe020-crash" in result
        assert "sig-fe020-backend" in result
        assert "TypeError" in result
        assert "tags" in result
        assert "前端" in result  # frontend tier label
        assert "后端" in result  # backend tier label

    def test_formats_perf020_evidence(self, perf020_state: DoctorState) -> None:
        """PERF-020 evidence includes N+1 and slow query details."""
        evidence = perf020_state.evidence
        result = format_evidence_for_agent(evidence)

        assert "sig-perf020-slow" in result
        assert "sig-perf020-n1" in result
        assert "4800ms" in result
        assert "N+1" in result

    def test_includes_context_summary(self, be020_state: DoctorState) -> None:
        """Evidence context includes signal details for real-time query architecture."""
        evidence = be020_state.evidence
        result = format_evidence_for_agent(evidence)

        assert "【实时查询信号】" in result
        assert "sig-be020-slow" in result
        assert "sig-be020-trace" in result

    def test_includes_user_report(self, be020_state: DoctorState) -> None:
        """User report is included at the top."""
        evidence = be020_state.evidence
        result = format_evidence_for_agent(evidence)

        assert "任务列表页面加载很慢" in result

    def test_includes_time_range_hint(self, be020_state: DoctorState) -> None:
        """Evidence with timestamp still formats correctly under real-time query architecture."""
        evidence = be020_state.evidence
        from datetime import datetime

        evidence.golden_signals[0].timestamp = datetime(2026, 6, 28, 10, 0, 0, tzinfo=UTC)

        result = format_evidence_for_agent(evidence)
        # New architecture: signals are formatted with IDs and source info
        assert "sig-be020-slow" in result
        assert "sig-be020-trace" in result
        assert "【实时查询信号】" in result


# ═════════════════════════════════════════════════════════════════════
# parse_diagnosis_report tests
# ═════════════════════════════════════════════════════════════════════


class TestParseDiagnosisReport:
    """Tests for parsing UnifiedAgent JSON output into DiagnosisReport."""

    def test_parses_be020_style_n1_report(self) -> None:
        """BE-020 N+1 report: affected_file, fix_suggestion, evidence_chain."""
        agent_result = {
            "messages": [
                AIMessage(
                    content="""
{
  "primary_category": "performance",
  "categories": ["performance", "backend_error"],
  "symptom_tier": "frontend",
  "root_cause_tier": "backend",
  "root_cause": "list_tasks 中存在 N+1 查询：对每个 task 单独查询 comments",
  "affected_file": "app/services/task_service.py",
  "affected_line": 42,
  "fix_suggestion": "【文件】app/services/task_service.py\\n【位置】第 42 行\\n【改前】for task in tasks: comments = await db.query(Comment).filter(Comment.task_id == task.id)\\n【改后】使用 selectinload(Task.comments) 预加载关联数据\\n【原因】N+1 查询导致 20 个 task 产生 21 次 DB 查询",
  "evidence_chain": ["sig-be020-slow", "sig-be020-trace", "span-be020-001"],
  "confidence": 0.92
}
"""
                ),
            ]
        }

        report = parse_diagnosis_report(agent_result)
        assert report is not None
        assert report.primary_category == "performance"
        assert "performance" in report.categories
        assert "backend_error" in report.categories
        assert report.symptom_tier == "frontend"
        assert report.root_cause_tier == "backend"
        assert "N+1" in report.root_cause
        assert report.affected_file == "app/services/task_service.py"
        assert report.affected_line == 42
        assert "selectinload" in report.fix_suggestion
        assert len(report.evidence_chain) == 3
        assert report.confidence == 0.92

    def test_parses_fe020_style_cross_layer_report(self) -> None:
        """FE-020 cross-layer report: symptom_tier != root_cause_tier."""
        agent_result = {
            "messages": [
                AIMessage(
                    content="""
{
  "primary_category": "backend_error",
  "categories": ["frontend_crash", "backend_error"],
  "symptom_tier": "frontend",
  "root_cause_tier": "backend",
  "root_cause": "后端 TaskResponse schema 缺少 tags 字段，导致前端访问 undefined 属性白屏",
  "affected_file": "app/schemas/task.py",
  "affected_line": 28,
  "fix_suggestion": "【文件】app/schemas/task.py\\n【位置】第 28 行\\n【改前】class TaskResponse(BaseModel): id: str; title: str\\n【改后】class TaskResponse(BaseModel): id: str; title: str; tags: list[str] = []\\n【原因】前端期望 tags 字段但后端未返回",
  "evidence_chain": ["sig-fe020-crash", "sig-fe020-backend", "corr-fe020-cross"],
  "confidence": 0.88
}
"""
                ),
            ]
        }

        report = parse_diagnosis_report(agent_result)
        assert report is not None
        assert report.symptom_tier == "frontend"
        assert report.root_cause_tier == "backend"  # cross-layer!
        assert report.primary_category == "backend_error"
        assert "frontend_crash" in report.categories
        assert "tags" in report.root_cause
        assert report.affected_file == "app/schemas/task.py"
        assert "TaskResponse" in report.fix_suggestion

    def test_parses_perf020_style_performance_report(self) -> None:
        """PERF-020 performance report: identifies N+1 and slow queries."""
        agent_result = {
            "messages": [
                AIMessage(
                    content="""
{
  "primary_category": "performance",
  "categories": ["performance"],
  "symptom_tier": "frontend",
  "root_cause_tier": "backend",
  "root_cause": "tasks 表缺少 status 列索引，导致全表扫描，配合 N+1 查询加剧性能恶化",
  "affected_file": "app/models/task.py",
  "affected_line": 15,
  "fix_suggestion": "【文件】app/models/task.py\\n【位置】第 15 行\\n【改前】status = Column(String)\\n【改后】status = Column(String, index=True)\\n【原因】WHERE status='pending' 触发全表扫描 4800ms",
  "evidence_chain": ["sig-perf020-slow", "sig-perf020-n1"],
  "confidence": 0.90
}
"""
                ),
            ]
        }

        report = parse_diagnosis_report(agent_result)
        assert report is not None
        assert report.primary_category == "performance"
        assert "N+1" in report.root_cause
        assert "索引" in report.root_cause or "index" in report.root_cause.lower()
        assert report.affected_file is not None
        assert report.confidence == 0.90

    def test_parses_raw_json_no_fence(self) -> None:
        """Raw JSON object (no markdown code fence) is parsed."""
        agent_result = {
            "messages": [
                AIMessage(
                    content=(
                        '{"primary_category":"backend_error",'
                        '"categories":["backend_error"],'
                        '"symptom_tier":"backend",'
                        '"root_cause_tier":"backend",'
                        '"root_cause":"Unhandled ValueError in task_service",'
                        '"affected_file":"app/services/task_service.py",'
                        '"affected_line":55,'
                        '"fix_suggestion":"Add try/except",'
                        '"evidence_chain":["sig-001"],'
                        '"confidence":0.85}'
                    )
                ),
            ]
        }

        report = parse_diagnosis_report(agent_result)
        assert report is not None
        assert report.primary_category == "backend_error"
        assert report.root_cause == "Unhandled ValueError in task_service"
        assert report.confidence == 0.85

    def test_falls_back_to_raw_text(self) -> None:
        """When no JSON found, raw text becomes root_cause with low confidence."""
        agent_result = {
            "messages": [
                AIMessage(content="The root cause is a database deadlock in the task update flow."),
            ]
        }

        report = parse_diagnosis_report(agent_result)
        assert report is not None
        assert "deadlock" in report.root_cause
        assert report.confidence == 0.2  # fallback confidence
        assert "JSON 解析失败" in report.notes

    def test_handles_empty_messages(self) -> None:
        """Agent result with no messages returns None."""
        report = parse_diagnosis_report({"messages": []})
        assert report is None

    def test_uses_last_ai_message_only(self) -> None:
        """Only the last AI message is used for parsing."""
        agent_result = {
            "messages": [
                AIMessage(content="Let me search the logs..."),
                AIMessage(
                    content="""
{
  "primary_category": "logic",
  "categories": ["logic"],
  "symptom_tier": "frontend",
  "root_cause_tier": "backend",
  "root_cause": "Race condition in task status update",
  "affected_file": "app/services/task_service.py",
  "affected_line": 88,
  "fix_suggestion": "Use SELECT FOR UPDATE",
  "evidence_chain": ["sig-race-001"],
  "confidence": 0.78
}
"""
                ),
            ]
        }

        report = parse_diagnosis_report(agent_result)
        assert report is not None
        assert "Race condition" in report.root_cause
        assert report.confidence == 0.78


# ═════════════════════════════════════════════════════════════════════
# extract_findings tests
# ═════════════════════════════════════════════════════════════════════


class TestExtractFindings:
    """Tests for extracting Finding records from agent messages."""

    def test_extracts_from_ai_message_with_json(self) -> None:
        """AIMessage with JSON summary is extracted as Finding."""
        agent_result = {
            "messages": [
                AIMessage(
                    content="""
Intermediate analysis:
```json
{"summary": "Detected N+1 pattern in list_tasks", "evidence_refs": ["sig-001"], "confidence": 0.8}
```
"""
                ),
                AIMessage(
                    content="""
Final diagnosis:
```json
{"summary": "Root cause: missing selectinload causes N+1", "evidence_refs": ["sig-001", "sig-002"], "confidence": 0.92}
```
"""
                ),
            ]
        }

        findings = extract_findings(agent_result)
        assert len(findings) >= 1
        assert findings[0].agent == "unified_agent"
        assert any("N+1" in f.summary for f in findings)

    def test_extracts_root_cause_as_summary(self) -> None:
        """Messages with root_cause field (not summary) are extracted."""
        agent_result = {
            "messages": [
                AIMessage(
                    content="""
{"root_cause": "Missing index on tasks.status", "evidence_chain": ["sig-001"], "confidence": 0.85}
"""
                ),
            ]
        }

        findings = extract_findings(agent_result)
        assert len(findings) >= 1
        assert "Missing index" in findings[0].summary

    def test_skips_tool_call_messages(self) -> None:
        """Messages with tool_calls are not treated as findings."""
        # Simulate an AIMessage with tool_calls attribute
        msg = AIMessage(content="")
        msg.tool_calls = [{"name": "search_observability", "args": {}}]  # type: ignore[attr-defined]

        agent_result = {"messages": [msg]}
        findings = extract_findings(agent_result)
        assert len(findings) == 0

    def test_handles_empty_messages(self) -> None:
        """Empty message list returns empty findings list."""
        findings = extract_findings({"messages": []})
        assert findings == []


# ═════════════════════════════════════════════════════════════════════
# Budget tracking tests
# ═════════════════════════════════════════════════════════════════════


class TestBudgetTracking:
    """Tests for update_budget and is_budget_exceeded."""

    def test_update_budget_counts_tokens(self) -> None:
        """Budget tracks estimated tokens from messages."""
        from datetime import datetime

        budget = BudgetState(started_at=datetime.now(UTC))
        agent_result = {
            "messages": [
                HumanMessage(content="Long evidence text " * 50),
                AIMessage(content="Analysis result " * 30),
            ]
        }

        updated = update_budget(budget, agent_result)
        assert updated.total_tokens > 0
        assert updated.tool_calls == 0

    def test_update_budget_counts_tool_calls(self) -> None:
        """Budget counts tool calls from AIMessages."""
        from datetime import datetime

        budget = BudgetState(started_at=datetime.now(UTC))
        msg = AIMessage(content="Calling tool...")
        msg.tool_calls = [  # type: ignore[attr-defined]
            {"name": "search_observability", "args": {"source": "loki", "query": "test"}},
            {"name": "code_search", "args": {"query": "list_tasks"}},
        ]

        updated = update_budget(budget, {"messages": [msg]})
        assert updated.tool_calls == 2

    def test_is_budget_exceeded_by_tool_calls(self) -> None:
        """Budget exceeded when tool calls >= 12."""
        budget = BudgetState(tool_calls=13, total_tokens=1000, elapsed_seconds=10)
        assert is_budget_exceeded(budget) is True

    def test_is_budget_exceeded_by_tokens(self) -> None:
        """Budget exceeded when tokens >= 100k."""
        budget = BudgetState(tool_calls=5, total_tokens=150_000, elapsed_seconds=30)
        assert is_budget_exceeded(budget) is True

    def test_is_budget_exceeded_by_time(self) -> None:
        """Budget exceeded when elapsed >= 300s."""
        budget = BudgetState(tool_calls=5, total_tokens=5000, elapsed_seconds=301)
        assert is_budget_exceeded(budget) is True

    def test_budget_not_exceeded_normal(self) -> None:
        """Normal usage does not exceed budget."""
        budget = BudgetState(tool_calls=6, total_tokens=50_000, elapsed_seconds=120)
        assert is_budget_exceeded(budget) is False


# ═════════════════════════════════════════════════════════════════════
# handle_agent_failure tests
# ═════════════════════════════════════════════════════════════════════


class TestHandleAgentFailure:
    """Tests for graceful agent failure handling."""

    def test_produces_fallback_report(self, be020_state: DoctorState) -> None:
        """Failure handler returns a best-effort report with early_stopped=True."""
        result = handle_agent_failure(be020_state, RuntimeError("LLM API timeout"))

        report = result["report"]
        assert isinstance(report, DiagnosisReport)
        assert "LLM API timeout" in report.root_cause
        assert result["early_stopped"] is True

        findings = result["findings"]
        assert len(findings) == 1
        assert findings[0].agent == "unified_agent"
        assert findings[0].confidence == 0.0

    def test_preserves_triage_category(self, be020_state: DoctorState) -> None:
        """V3: Fallback report uses empty category (triage node removed in V3).

        In V3, classification is embedded in the unified_agent System Prompt,
        not in a separate triage node. When the agent fails, there is no
        triage output to fall back to — primary_category defaults to empty.
        """
        result = handle_agent_failure(be020_state, Exception("generic error"))

        report = result["report"]
        # V3: triage node removed → no category available on failure
        assert report.primary_category == ""


# ═════════════════════════════════════════════════════════════════════
# unified_agent_node integration tests (mocked agent)
# ═════════════════════════════════════════════════════════════════════


class TestUnifiedAgentNode:
    """Tests for the UnifiedAgent node function with mocked agent."""

    @patch(
        "src.graph.subgraphs.unified_agent.get_unified_agent",
        autospec=True,
    )
    async def test_skips_when_no_evidence(
        self, mock_get_agent: MagicMock, empty_state: DoctorState
    ) -> None:
        """Node returns fallback report when agent fails with empty evidence."""
        mock_agent = AsyncMock()
        mock_agent.ainvoke = AsyncMock(side_effect=RuntimeError("No evidence available"))
        mock_get_agent.return_value = mock_agent

        result = await unified_agent_node(empty_state)

        report = result["report"]
        assert isinstance(report, DiagnosisReport)
        assert "证据不足" in report.root_cause or report.confidence == 0.0

        findings = result["findings"]
        assert len(findings) >= 1

    @patch(
        "src.graph.subgraphs.unified_agent.get_unified_agent",
        autospec=True,
    )
    async def test_be020_diagnosis_n1_affected_file(
        self, mock_get_agent: MagicMock, be020_state: DoctorState
    ) -> None:
        """
        BE-020 N+1 case: report includes affected_file and fix_suggestion.

        Verifies the agent correctly identifies the N+1 query pattern and
        produces a code-level fix.
        """
        mock_agent = AsyncMock()
        mock_agent.ainvoke = AsyncMock(
            return_value={
                "messages": [
                    AIMessage(
                        content="""
{
  "primary_category": "performance",
  "categories": ["performance", "backend_error"],
  "symptom_tier": "frontend",
  "root_cause_tier": "backend",
  "root_cause": "list_tasks N+1: 对每个 task 单独查询 comments 导致 20 次额外 DB 查询",
  "affected_file": "app/services/task_service.py",
  "affected_line": 42,
  "fix_suggestion": "【文件】app/services/task_service.py\\n【位置】第 42 行\\n【改前】for task in tasks: comments = await db.query(Comment).filter(Comment.task_id == task.id)\\n【改后】tasks = await db.query(Task).options(selectinload(Task.comments)).all()\\n【原因】N+1 查询模式",
  "evidence_chain": ["sig-be020-slow", "sig-be020-trace"],
  "confidence": 0.92
}
"""
                    ),
                ]
            }
        )
        mock_get_agent.return_value = mock_agent

        result = await unified_agent_node(be020_state)

        # Verify agent was called with formatted evidence
        mock_agent.ainvoke.assert_called_once()
        call_args = mock_agent.ainvoke.call_args[0][0]
        messages = call_args["messages"]
        assert len(messages) == 1
        assert isinstance(messages[0], HumanMessage)
        assert "sig-be020-slow" in str(messages[0].content)

        # Verify report has affected_file and fix_suggestion
        report = result["report"]
        assert report is not None
        assert report.affected_file == "app/services/task_service.py"
        assert report.affected_line == 42
        assert "selectinload" in report.fix_suggestion
        assert report.confidence == 0.92

    @patch(
        "src.graph.subgraphs.unified_agent.get_unified_agent",
        autospec=True,
    )
    async def test_fe020_cross_layer_diagnosis(
        self, mock_get_agent: MagicMock, fe020_state: DoctorState
    ) -> None:
        """
        FE-020 cross-layer case: report identifies cross-layer root cause.

        Symptom is frontend (TypeError/white screen), but root cause is
        backend (missing field in API response).
        """
        mock_agent = AsyncMock()
        mock_agent.ainvoke = AsyncMock(
            return_value={
                "messages": [
                    AIMessage(
                        content="""
{
  "primary_category": "backend_error",
  "categories": ["frontend_crash", "backend_error"],
  "symptom_tier": "frontend",
  "root_cause_tier": "backend",
  "root_cause": "TaskResponse schema 缺少 tags 字段导致前端 TypeError 白屏",
  "affected_file": "app/schemas/task.py",
  "affected_line": 28,
  "fix_suggestion": "【文件】app/schemas/task.py\\n【位置】第 28 行\\n【改前】class TaskResponse(BaseModel): id: str; title: str\\n【改后】class TaskResponse(BaseModel): id: str; title: str; tags: list[str] = []",
  "evidence_chain": ["sig-fe020-crash", "sig-fe020-backend", "corr-fe020-cross"],
  "confidence": 0.88
}
"""
                    ),
                ]
            }
        )
        mock_get_agent.return_value = mock_agent

        result = await unified_agent_node(fe020_state)

        report = result["report"]
        assert report is not None
        # Cross-layer: symptom at frontend, root cause at backend
        assert report.symptom_tier == "frontend"
        assert report.root_cause_tier == "backend"
        assert "frontend_crash" in report.categories
        assert "backend_error" in report.categories
        assert "tags" in report.root_cause.lower()

    @patch(
        "src.graph.subgraphs.unified_agent.get_unified_agent",
        autospec=True,
    )
    async def test_perf020_n1_identification(
        self, mock_get_agent: MagicMock, perf020_state: DoctorState
    ) -> None:
        """
        PERF-020 performance case: report identifies N+1 and gives fix.

        Verifies the agent correctly classifies a performance bug and
        references N+1 / index in the diagnosis.
        """
        mock_agent = AsyncMock()
        mock_agent.ainvoke = AsyncMock(
            return_value={
                "messages": [
                    AIMessage(
                        content="""
{
  "primary_category": "performance",
  "categories": ["performance"],
  "symptom_tier": "frontend",
  "root_cause_tier": "backend",
  "root_cause": "tasks 表缺少 status 列索引 + N+1 查询导致全表扫描 4800ms",
  "affected_file": "app/models/task.py",
  "affected_line": 15,
  "fix_suggestion": "【文件】app/models/task.py\\n【位置】第 15 行\\n【改前】status = Column(String)\\n【改后】status = Column(String, index=True)\\n【原因】无索引导致全表扫描",
  "evidence_chain": ["sig-perf020-slow", "sig-perf020-n1"],
  "confidence": 0.90
}
"""
                    ),
                ]
            }
        )
        mock_get_agent.return_value = mock_agent

        result = await unified_agent_node(perf020_state)

        report = result["report"]
        assert report is not None
        assert report.primary_category == "performance"
        assert "N+1" in report.root_cause
        assert report.affected_file is not None
        assert "index" in report.fix_suggestion.lower() or "索引" in report.fix_suggestion

    @patch(
        "src.graph.subgraphs.unified_agent.get_unified_agent",
        autospec=True,
    )
    async def test_handles_agent_error(
        self, mock_get_agent: MagicMock, be020_state: DoctorState
    ) -> None:
        """Node returns fallback report when agent raises an exception."""
        mock_agent = AsyncMock()
        mock_agent.ainvoke = AsyncMock(side_effect=RuntimeError("LLM API timeout after 30s"))
        mock_get_agent.return_value = mock_agent

        result = await unified_agent_node(be020_state)

        report = result["report"]
        assert isinstance(report, DiagnosisReport)
        assert "LLM API timeout" in report.root_cause
        assert result["early_stopped"] is True

    @patch(
        "src.graph.subgraphs.unified_agent.get_unified_agent",
        autospec=True,
    )
    async def test_sets_early_stopped_on_budget_exceeded(
        self, mock_get_agent: MagicMock, be020_state: DoctorState
    ) -> None:
        """
        When budget is already near limit, early_stopped is propagated.

        The node should check budget after agent returns and set early_stopped
        if the combined usage exceeds limits.
        """
        # Pre-set budget near the limit
        from datetime import datetime

        be020_state.budget = BudgetState(
            tool_calls=10,  # 2 more calls from agent will exceed 12
            total_tokens=0,
            started_at=datetime.now(UTC),
        )

        mock_agent = AsyncMock()
        # Agent makes 3 more tool calls (total = 13 > 12)
        msg = AIMessage(
            content='{"primary_category":"performance","root_cause":"N+1","confidence":0.7}'
        )
        msg.tool_calls = [  # type: ignore[attr-defined]
            {"name": "search_observability", "args": {}},
            {"name": "code_search", "args": {}},
            {"name": "get_file_content", "args": {}},
        ]
        mock_agent.ainvoke = AsyncMock(return_value={"messages": [msg]})
        mock_get_agent.return_value = mock_agent

        result = await unified_agent_node(be020_state)

        assert result["early_stopped"] is True
        report = result["report"]
        assert report.early_stopped is True
