"""Unit tests for KnowledgeService (hybrid_service)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.documents import Document

from src.graph.state import DiagnosisReport, Evidence
from src.knowledge.hybrid_service import (
    KnowledgeService,
    get_knowledge_service,
    reset_knowledge_service,
)


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    """Reset the singleton before each test."""
    reset_knowledge_service()


@pytest.fixture
def mock_vector_kb() -> MagicMock:
    """Mock VectorKnowledgeBase."""
    mock = MagicMock()
    mock.search = AsyncMock(return_value=[])
    mock.add_documents = AsyncMock(return_value=["id1"])
    mock.get_collection = MagicMock()
    return mock


@pytest.fixture
def mock_struct_kb() -> MagicMock:
    """Mock StructKnowledgeBase."""
    mock = MagicMock()
    mock.match_error_pattern = MagicMock(return_value=None)
    mock.query_framework_practices = MagicMock(return_value=[])
    return mock


@pytest.fixture
def svc(mock_vector_kb: MagicMock, mock_struct_kb: MagicMock) -> KnowledgeService:
    """Create a KnowledgeService with mocked backends."""
    with patch(
        "src.knowledge.hybrid_service.VectorKnowledgeBase",
        return_value=mock_vector_kb,
    ), patch(
        "src.knowledge.hybrid_service.StructKnowledgeBase",
        return_value=mock_struct_kb,
    ), patch(
        "src.knowledge.hybrid_service.get_embeddings",
        return_value=MagicMock(),
    ):
        return KnowledgeService(
            qdrant_url="http://localhost:6333",
            struct_db_path=":memory:",
        )


class TestSearchHistoricalCases:
    """Tests for search_historical_cases."""

    @pytest.mark.asyncio
    async def test_returns_formatted_results(
        self, svc: KnowledgeService, mock_vector_kb: MagicMock
    ) -> None:
        """Should return properly formatted dicts from vector search."""
        doc = Document(
            page_content="Root cause: N+1 query in task listing",
            metadata={
                "case_id": "case-001",
                "category": "performance",
                "affected_file": "tasks.py",
                "confidence": 0.85,
                "timestamp": "2026-06-15T10:00:00",
                "_score": 0.92,
            },
        )
        mock_vector_kb.search.return_value = [doc]

        results = await svc.search_historical_cases("slow task list", k=3)

        assert len(results) == 1
        assert results[0]["case_id"] == "case-001"
        assert results[0]["category"] == "performance"
        assert results[0]["root_cause"] == "Root cause: N+1 query in task listing"
        assert results[0]["similarity_score"] == 0.92

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_results(
        self, svc: KnowledgeService, mock_vector_kb: MagicMock
    ) -> None:
        """Should return empty list when vector search returns nothing."""
        mock_vector_kb.search.return_value = []

        results = await svc.search_historical_cases("unknown issue")
        assert results == []


class TestSearchPractices:
    """Tests for search_practices."""

    @pytest.mark.asyncio
    async def test_exact_match_first(
        self, svc: KnowledgeService, mock_struct_kb: MagicMock
    ) -> None:
        """Should try exact framework+practice_type match first."""
        expected = [
            {"framework": "FastAPI", "practice_type": "performance", "description": "Use async"}
        ]
        mock_struct_kb.query_framework_practices.return_value = expected

        results = await svc.search_practices("FastAPI", "performance")

        # Should be called with both filters first
        first_call = mock_struct_kb.query_framework_practices.call_args_list[0]
        assert first_call[1]["framework"] == "FastAPI"
        assert first_call[1]["practice_type"] == "performance"
        assert results == expected

    @pytest.mark.asyncio
    async def test_fallback_to_framework_only(
        self, svc: KnowledgeService, mock_struct_kb: MagicMock
    ) -> None:
        """Should fall back to framework-only when exact match yields nothing."""
        # First call (exact) returns empty; second (framework-only) returns results
        mock_struct_kb.query_framework_practices.side_effect = [
            [],
            [{"framework": "FastAPI", "practice_type": "async", "description": "Use async"}],
        ]

        results = await svc.search_practices("FastAPI", "nonexistent_type")

        assert len(results) == 1
        assert results[0]["framework"] == "FastAPI"

    @pytest.mark.asyncio
    async def test_fallback_to_practice_type_only(
        self, svc: KnowledgeService, mock_struct_kb: MagicMock
    ) -> None:
        """Should fall back to practice_type-only as last resort."""
        mock_struct_kb.query_framework_practices.side_effect = [
            [],  # exact match
            [],  # framework only
            [{"framework": "React", "practice_type": "error_handling", "description": "Use ErrorBoundary"}],
        ]

        results = await svc.search_practices("UnknownFramework", "error_handling")

        assert len(results) == 1
        assert results[0]["practice_type"] == "error_handling"


class TestClassifyErrorPattern:
    """Tests for classify_error_pattern."""

    @pytest.mark.asyncio
    async def test_regex_match_returns_category(
        self, svc: KnowledgeService, mock_struct_kb: MagicMock
    ) -> None:
        """Should return category from regex match."""
        mock_struct_kb.match_error_pattern.return_value = {
            "pattern": "TypeError",
            "category": "frontend_crash",
            "description": "JS type error",
        }

        result = await svc.classify_error_pattern("TypeError: Cannot read property")
        assert result == "frontend_crash"

    @pytest.mark.asyncio
    async def test_vector_fallback_when_no_regex_match(
        self, svc: KnowledgeService, mock_struct_kb: MagicMock, mock_vector_kb: MagicMock
    ) -> None:
        """Should fall back to vector search when regex yields no match."""
        mock_struct_kb.match_error_pattern.return_value = None
        doc = Document(
            page_content="some error",
            metadata={"category": "backend_error"},
        )
        mock_vector_kb.search.return_value = [doc]

        result = await svc.classify_error_pattern("Some unknown error message")

        assert result == "backend_error"

    @pytest.mark.asyncio
    async def test_returns_none_when_both_fail(
        self, svc: KnowledgeService, mock_struct_kb: MagicMock, mock_vector_kb: MagicMock
    ) -> None:
        """Should return None when both regex and vector fail."""
        mock_struct_kb.match_error_pattern.return_value = None
        mock_vector_kb.search.return_value = []

        result = await svc.classify_error_pattern("totally unknown")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_vector_raises(
        self, svc: KnowledgeService, mock_struct_kb: MagicMock, mock_vector_kb: MagicMock
    ) -> None:
        """Should return None gracefully when vector search raises."""
        mock_struct_kb.match_error_pattern.return_value = None
        mock_vector_kb.search.side_effect = Exception("Qdrant down")

        result = await svc.classify_error_pattern("some error")
        assert result is None


class TestIndexDiagnosis:
    """Tests for index_diagnosis."""

    @pytest.mark.asyncio
    async def test_indexes_high_confidence_diagnosis(
        self, svc: KnowledgeService, mock_vector_kb: MagicMock
    ) -> None:
        """Should index a diagnosis with confidence >= 0.6."""
        report = DiagnosisReport(
            bug_category="performance",
            root_cause="N+1 query in task listing",
            affected_file="tasks.py",
            affected_line=42,
            fix_suggestion="Use selectinload",
            evidence_chain=["log analysis", "trace analysis"],
            confidence=0.85,
        )
        evidence = Evidence(user_report="Task list is very slow")

        await svc.index_diagnosis(report, evidence)

        mock_vector_kb.add_documents.assert_called_once()
        docs = mock_vector_kb.add_documents.call_args[0][1]
        assert len(docs) == 1
        doc = docs[0]
        assert "N+1 query" in doc.page_content
        assert doc.metadata["category"] == "performance"
        assert doc.metadata["confidence"] == 0.85

    @pytest.mark.asyncio
    async def test_skips_low_confidence_diagnosis(
        self, svc: KnowledgeService, mock_vector_kb: MagicMock
    ) -> None:
        """Should skip indexing when confidence < 0.6."""
        report = DiagnosisReport(
            bug_category="backend_error",
            root_cause="Unknown",
            confidence=0.3,
        )
        evidence = Evidence(user_report="Something went wrong")

        await svc.index_diagnosis(report, evidence)

        mock_vector_kb.add_documents.assert_not_called()

    @pytest.mark.asyncio
    async def test_respects_skip_index_flag(
        self, svc: KnowledgeService, mock_vector_kb: MagicMock
    ) -> None:
        """Should skip indexing when skip_index=True (evaluation data leakage prevention)."""
        report = DiagnosisReport(
            bug_category="backend_error",
            root_cause="Known issue",
            confidence=0.9,
        )
        evidence = Evidence(user_report="test")

        await svc.index_diagnosis(report, evidence, skip_index=True)

        mock_vector_kb.add_documents.assert_not_called()

    @pytest.mark.asyncio
    async def test_document_content_includes_user_report_and_fix(
        self, svc: KnowledgeService, mock_vector_kb: MagicMock
    ) -> None:
        """Should include user_report, root_cause, and fix in the document content."""
        report = DiagnosisReport(
            bug_category="frontend_crash",
            root_cause="Null assignee access",
            fix_suggestion="Use optional chaining ?.",
            confidence=0.8,
        )
        evidence = Evidence(user_report="Page crashes when viewing task")

        await svc.index_diagnosis(report, evidence)

        docs = mock_vector_kb.add_documents.call_args[0][1]
        content = docs[0].page_content
        assert "Page crashes when viewing task" in content
        assert "Null assignee access" in content
        assert "optional chaining" in content


class TestSearchCode:
    """Tests for search_code."""

    @pytest.mark.asyncio
    async def test_delegates_to_vector_search(
        self, svc: KnowledgeService, mock_vector_kb: MagicMock
    ) -> None:
        """Should delegate to vector_kb.search with code_index collection."""
        expected = [Document(page_content="def list_tasks(...)", metadata={"file": "tasks.py"})]
        mock_vector_kb.search.return_value = expected

        results = await svc.search_code("list tasks endpoint", k=5)

        call_args = mock_vector_kb.search.call_args
        assert call_args[0][0] == "code_index"
        assert call_args[0][1] == "list tasks endpoint"
        assert results == expected


class TestSingleton:
    """Tests for the module-level singleton."""

    def test_get_knowledge_service_creates_singleton(self) -> None:
        """Should create and cache a KnowledgeService singleton."""
        with patch(
            "src.knowledge.hybrid_service.VectorKnowledgeBase",
            return_value=MagicMock(),
        ), patch(
            "src.knowledge.hybrid_service.StructKnowledgeBase",
            return_value=MagicMock(),
        ), patch(
            "src.knowledge.hybrid_service.get_embeddings",
            return_value=MagicMock(),
        ):
            svc1 = get_knowledge_service()
            svc2 = get_knowledge_service()
            assert svc1 is svc2

    def test_reset_knowledge_service(self) -> None:
        """Should reset the singleton."""
        with patch(
            "src.knowledge.hybrid_service.VectorKnowledgeBase",
            return_value=MagicMock(),
        ), patch(
            "src.knowledge.hybrid_service.StructKnowledgeBase",
            return_value=MagicMock(),
        ), patch(
            "src.knowledge.hybrid_service.get_embeddings",
            return_value=MagicMock(),
        ):
            svc1 = get_knowledge_service()
            reset_knowledge_service()
            svc2 = get_knowledge_service()
            assert svc1 is not svc2
