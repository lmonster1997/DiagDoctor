"""
KnowledgeService — hybrid knowledge retrieval combining vector + structured KB.

Provides the main interface used by diagnosis agents:
- Search historical cases (vector)
- Search framework best practices (structured)
- Classify error patterns (structured regex + vector)
- Index completed diagnoses (vector)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from langchain_core.documents import Document

from src.config import settings
from src.graph.state import DiagnosisReport, Evidence
from src.knowledge.embeddings import get_embeddings
from src.knowledge.struct_kb import StructKnowledgeBase
from src.knowledge.vector_kb import VectorKnowledgeBase

logger = logging.getLogger(__name__)

# Qdrant collection names
COLLECTION_HISTORICAL_CASES = "historical_cases"
COLLECTION_CODE_INDEX = "code_index"


class KnowledgeService:
    """
    Hybrid knowledge service combining vector search and structured lookup.

    This is the primary interface for diagnosis Agents to retrieve relevant
    historical cases, framework best practices, and error pattern classifications.

    Usage::

        svc = KnowledgeService()
        cases = await svc.search_historical_cases("N+1 query slow", k=3)
        practices = await svc.search_practices("FastAPI", "performance")
        category = await svc.classify_error_pattern("TypeError: Cannot read property")
    """

    def __init__(
        self,
        qdrant_url: str | None = None,
        struct_db_path: str | None = None,
    ) -> None:
        """
        Initialize the knowledge service.

        Args:
            qdrant_url: Qdrant server URL (defaults to settings.qdrant_url).
            struct_db_path: Path to SQLite DB (defaults to 'data/struct_kb.db').
        """
        qdrant_url = qdrant_url or settings.qdrant_url

        self._vector_kb: VectorKnowledgeBase = VectorKnowledgeBase(
            qdrant_url=qdrant_url,
            embeddings=get_embeddings(),
        )
        self._struct_kb: StructKnowledgeBase = StructKnowledgeBase(
            db_path=struct_db_path or "data/struct_kb.db"
        )

    # ── Search: Historical Cases (Vector) ────────────────────────────

    async def search_historical_cases(self, query: str, k: int = 3) -> list[dict[str, Any]]:
        """
        Search previously diagnosed cases similar to the query.

        Args:
            query: Natural language description of the current bug.
            k: Number of similar cases to return.

        Returns:
            List of dicts with keys: case_id, category, root_cause, affected_file,
            confidence, timestamp, similarity_score.
        """
        docs = await self._vector_kb.search(COLLECTION_HISTORICAL_CASES, query, k=k)

        results: list[dict[str, Any]] = []
        for doc in docs:
            meta = doc.metadata or {}
            results.append(
                {
                    "case_id": meta.get("case_id", ""),
                    "category": meta.get("category", ""),
                    "root_cause": doc.page_content,
                    "affected_file": meta.get("affected_file"),
                    "confidence": meta.get("confidence", 0.0),
                    "timestamp": meta.get("timestamp", ""),
                    "similarity_score": meta.get("_score", 0.0),
                }
            )

        logger.debug("Historical case search for '%s' returned %d results", query, len(results))
        return results

    # ── Search: Framework Practices (Structured) ────────────────────

    async def search_practices(self, framework: str, problem: str) -> list[dict[str, Any]]:
        """
        Search framework best practices relevant to a given problem.

        Args:
            framework: Framework name (e.g. 'FastAPI', 'React').
            problem: Problem description or type (e.g. 'performance', 'security').

        Returns:
            List of matching practice dicts.
        """
        # First try exact framework + practice_type match
        results = self._struct_kb.query_framework_practices(
            framework=framework,
            practice_type=problem,
        )

        # If no exact match, try framework-only
        if not results:
            results = self._struct_kb.query_framework_practices(
                framework=framework,
            )

        # If still nothing, try problem as practice_type
        if not results:
            results = self._struct_kb.query_framework_practices(
                practice_type=problem,
            )

        logger.debug(
            "Practice search for framework='%s', problem='%s' returned %d results",
            framework,
            problem,
            len(results),
        )
        return results

    # ── Classify: Error Pattern ─────────────────────────────────────

    async def classify_error_pattern(self, error_message: str) -> str | None:
        """
        Classify an error message by matching against known patterns.

        First tries structured regex patterns (fast), falls back to
        vector similarity search if no regex match.

        Args:
            error_message: The error message string to classify.

        Returns:
            Bug category string (e.g. 'frontend_crash', 'backend_error') or None.
        """
        # Step 1: Try structured regex match
        match = self._struct_kb.match_error_pattern(error_message)
        if match:
            logger.debug(
                "Error pattern matched via regex: %s → %s", error_message[:80], match["category"]
            )
            return str(match["category"])

        # Step 2: Fall back to vector similarity against historical cases
        try:
            docs = await self._vector_kb.search(
                COLLECTION_HISTORICAL_CASES,
                error_message,
                k=1,
            )
            if docs:
                category = docs[0].metadata.get("category")
                if category:
                    logger.debug("Error pattern classified via vector: %s", category)
                    return str(category)
        except Exception as exc:
            logger.warning("Vector fallback for error classification failed: %s", exc)

        return None

    # ── Index: Store Diagnosis ──────────────────────────────────────

    async def index_diagnosis(
        self,
        report: DiagnosisReport,
        evidence: Evidence,
        skip_index: bool = False,
    ) -> None:
        """
        Index a completed diagnosis into the historical cases vector store.

        Only indexes diagnoses with confidence >= 0.6 unless overridden.

        Args:
            report: The final DiagnosisReport.
            evidence: The original Evidence for this case.
            skip_index: If True, skip indexing (e.g. for evaluation cases to avoid data leakage).
        """
        if skip_index:
            logger.debug("Skipping index for case (skip_index=True)")
            return

        if report.confidence < 0.6:
            logger.debug(
                "Skipping index for low-confidence diagnosis (%.2f < 0.6)",
                report.confidence,
            )
            return

        # Build document content: user_report + root_cause for semantic search
        content_parts = []
        if evidence.user_report:
            content_parts.append(f"User Report: {evidence.user_report}")
        content_parts.append(f"Root Cause: {report.root_cause}")
        if report.fix_suggestion:
            content_parts.append(f"Fix: {report.fix_suggestion}")

        doc = Document(
            page_content="\n".join(content_parts),
            metadata={
                "category": report.bug_category,
                "root_cause": report.root_cause,
                "affected_file": report.affected_file,
                "affected_line": report.affected_line,
                "fix_suggestion": report.fix_suggestion,
                "confidence": report.confidence,
                "evidence_chain": report.evidence_chain,
                "timestamp": datetime.utcnow().isoformat(),
            },
        )

        await self._vector_kb.add_documents(COLLECTION_HISTORICAL_CASES, [doc])
        logger.info(
            "Indexed diagnosis: category=%s, confidence=%.2f",
            report.bug_category,
            report.confidence,
        )

    # ── Code Search (placeholder for code_index) ────────────────────

    async def search_code(self, query: str, k: int = 5) -> list[Document]:
        """
        Search the code index (vector) for relevant code chunks.

        Args:
            query: Natural language or code snippet to search for.
            k: Number of results.

        Returns:
            Matching Document objects with code chunk metadata.
        """
        return await self._vector_kb.search(COLLECTION_CODE_INDEX, query, k=k)

    # ── Resource management ─────────────────────────────────────────

    def close(self) -> None:
        """Close all underlying connections."""
        self._vector_kb.close()
        self._struct_kb.close()


# ── Module-level singleton (lazy) ────────────────────────────────────

_knowledge_service: KnowledgeService | None = None


def get_knowledge_service() -> KnowledgeService:
    """Get or create the singleton KnowledgeService instance."""
    global _knowledge_service
    if _knowledge_service is None:
        _knowledge_service = KnowledgeService()
    return _knowledge_service


def reset_knowledge_service() -> None:
    """Reset the singleton (useful for testing)."""
    global _knowledge_service
    if _knowledge_service is not None:
        _knowledge_service.close()
    _knowledge_service = None
