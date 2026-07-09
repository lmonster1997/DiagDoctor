"""Unit tests for VectorKnowledgeBase."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.documents import Document

from src.knowledge.vector_kb import DEFAULT_VECTOR_SIZE, VectorKnowledgeBase


@pytest.fixture
def mock_embeddings() -> MagicMock:
    """Mock LangChain Embeddings."""
    return MagicMock()


@pytest.fixture
def mock_qdrant_client() -> MagicMock:
    """Mock QdrantClient."""
    client = MagicMock()
    # get_collection should not raise by default (collection exists)
    client.get_collection.return_value = MagicMock()
    return client


@pytest.fixture
def vkb(mock_embeddings: MagicMock, mock_qdrant_client: MagicMock):
    """Create a VectorKnowledgeBase with mocked QdrantClient and QdrantVectorStore."""
    with (
        patch(
            "src.knowledge.vector_kb.QdrantClient",
            return_value=mock_qdrant_client,
        ),
        patch(
            "src.knowledge.vector_kb.QdrantVectorStore",
        ) as mock_store_cls,
    ):
        # Make QdrantVectorStore() return a configured MagicMock
        mock_store = MagicMock()
        mock_store_cls.return_value = mock_store
        kb = VectorKnowledgeBase(
            qdrant_url="http://localhost:6333",
            embeddings=mock_embeddings,
        )
        yield kb


class TestGetCollection:
    """Tests for get_collection method."""

    def test_returns_existing_collection(self, vkb: VectorKnowledgeBase) -> None:
        """Should return cached store when collection already exists."""
        store1 = vkb.get_collection("test_coll")
        store2 = vkb.get_collection("test_coll")

        assert store1 is store2  # cached

    def test_creates_collection_if_not_exists(
        self, vkb: VectorKnowledgeBase, mock_qdrant_client: MagicMock
    ) -> None:
        """Should create collection if it doesn't exist."""
        from qdrant_client.http.exceptions import ResponseHandlingException

        # Make get_collection fail first (not found), then succeed (after creation)
        call_count = 0

        def get_collection_side_effect(collection_name: str) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ResponseHandlingException("not found")
            return MagicMock()

        mock_qdrant_client.get_collection.side_effect = get_collection_side_effect

        # Clear cache so get_collection re-initializes
        vkb._stores.clear()

        vkb.get_collection("new_coll")

        mock_qdrant_client.create_collection.assert_called_once()
        call_kwargs = mock_qdrant_client.create_collection.call_args[1]
        assert call_kwargs["collection_name"] == "new_coll"
        assert call_kwargs["vectors_config"].size == DEFAULT_VECTOR_SIZE


class TestAddDocuments:
    """Tests for add_documents methods."""

    def test_add_documents_sync(self, vkb: VectorKnowledgeBase) -> None:
        """Should add documents synchronously."""
        docs = [Document(page_content="test doc", metadata={"key": "val"})]
        store = vkb.get_collection("test_coll")
        store.add_documents.return_value = ["id1"]

        ids = vkb.add_documents_sync("test_coll", docs)
        assert ids == ["id1"]
        store.add_documents.assert_called_once_with(docs)

    @pytest.mark.asyncio
    async def test_add_documents_async(self, vkb: VectorKnowledgeBase) -> None:
        """Should add documents asynchronously."""
        docs = [Document(page_content="test doc")]
        store = vkb.get_collection("test_coll")
        store.aadd_documents = AsyncMock(return_value=["id1"])

        ids = await vkb.add_documents("test_coll", docs)
        assert ids == ["id1"]


class TestSearch:
    """Tests for search methods."""

    def test_search_sync_no_filters(self, vkb: VectorKnowledgeBase) -> None:
        """Should search without filters."""
        expected_docs = [Document(page_content="result", metadata={"_score": 0.9})]
        store = vkb.get_collection("test_coll")
        store.similarity_search.return_value = expected_docs

        results = vkb.search_sync("test_coll", "query", k=3)
        store.similarity_search.assert_called_once_with("query", k=3)
        assert results == expected_docs

    def test_search_sync_with_filters(self, vkb: VectorKnowledgeBase) -> None:
        """Should search with Qdrant filters."""
        filters = {"must": [{"key": "category", "match": {"value": "backend_error"}}]}
        store = vkb.get_collection("test_coll")
        store.similarity_search.return_value = []

        vkb.search_sync("test_coll", "query", k=5, filters=filters)
        store.similarity_search.assert_called_once_with("query", k=5, filter=filters)

    @pytest.mark.asyncio
    async def test_search_async(self, vkb: VectorKnowledgeBase) -> None:
        """Should search asynchronously."""
        expected_docs = [Document(page_content="async result")]
        store = vkb.get_collection("test_coll")
        store.asimilarity_search = AsyncMock(return_value=expected_docs)

        results = await vkb.search("test_coll", "async query", k=5)
        assert results == expected_docs


class TestDeleteCollection:
    """Tests for delete_collection method."""

    def test_delete_existing_collection(
        self, vkb: VectorKnowledgeBase, mock_qdrant_client: MagicMock
    ) -> None:
        """Should delete an existing collection and clear cache."""
        # First, ensure the collection is cached
        vkb.get_collection("to_delete")

        vkb.delete_collection("to_delete")

        mock_qdrant_client.delete_collection.assert_called_once_with("to_delete")
        # Cache should be cleared
        assert "to_delete" not in vkb._stores

    def test_delete_nonexistent_collection_no_error(
        self, vkb: VectorKnowledgeBase, mock_qdrant_client: MagicMock
    ) -> None:
        """Should not raise when deleting a non-existent collection."""
        from qdrant_client.http.exceptions import ResponseHandlingException

        mock_qdrant_client.delete_collection.side_effect = ResponseHandlingException("not found")

        # Should not raise
        vkb.delete_collection("nonexistent")
