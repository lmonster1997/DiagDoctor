"""
VectorKnowledgeBase — langchain-qdrant wrapper for vector storage and retrieval.

Provides collection CRUD + semantic search over document embeddings.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_qdrant import QdrantVectorStore
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from qdrant_client.http.models import Distance, VectorParams

logger = logging.getLogger(__name__)

# Default vector size for DashScope text-embedding-v3 = 1024
# OpenAI text-embedding-3-small = 1536; bge-m3 = 1024.
DEFAULT_VECTOR_SIZE = 1024
DEFAULT_DISTANCE = Distance.COSINE


class VectorKnowledgeBase:
    """
    Wrapper around langchain-qdrant for managing vector collections.

    Usage::

        kb = VectorKnowledgeBase(qdrant_url="http://localhost:6333", embeddings=get_embeddings())
        kb.add_documents("my_collection", [Document(page_content="hello")])
        results = kb.search("my_collection", "hello", k=5)
    """

    def __init__(self, qdrant_url: str, embeddings: Embeddings):
        """
        Initialize the vector knowledge base.

        Args:
            qdrant_url: URL of the Qdrant server.
            embeddings: LangChain Embeddings instance.
        """
        self.qdrant_url = qdrant_url
        self.embeddings = embeddings

        # Build QdrantClient (prefer gRPC on 6334 if not explicitly HTTP)
        self._client = QdrantClient(url=qdrant_url, prefer_grpc=False, timeout=30)

        # Cached QdrantVectorStore instances keyed by collection name
        self._stores: dict[str, QdrantVectorStore] = {}

    # ── Public API ──────────────────────────────────────────────────

    def get_collection(self, name: str) -> QdrantVectorStore:
        """
        Get or create a Qdrant collection as a QdrantVectorStore.

        If the collection does not exist, it is created automatically.

        Args:
            name: Collection name.

        Returns:
            QdrantVectorStore instance.
        """
        if name in self._stores:
            return self._stores[name]

        # Ensure collection exists
        self._ensure_collection(name)

        store = QdrantVectorStore(
            client=self._client,
            collection_name=name,
            embedding=self.embeddings,
        )
        self._stores[name] = store
        return store

    async def add_documents(self, collection: str, docs: list[Document]) -> list[str]:
        """
        Add documents to a collection (async).

        Args:
            collection: Target collection name.
            docs: List of LangChain Documents to embed and store.

        Returns:
            List of document IDs assigned by Qdrant.
        """
        store = self.get_collection(collection)
        ids: list[str] = await store.aadd_documents(docs)
        logger.info("Added %d documents to collection '%s'", len(docs), collection)
        return ids

    def add_documents_sync(self, collection: str, docs: list[Document]) -> list[str]:
        """
        Add documents to a collection (synchronous).

        Args:
            collection: Target collection name.
            docs: List of LangChain Documents to embed and store.

        Returns:
            List of document IDs assigned by Qdrant.
        """
        store = self.get_collection(collection)
        ids: list[str] = store.add_documents(docs)
        logger.info("Added %d documents to collection '%s'", len(docs), collection)
        return ids

    async def search(
        self,
        collection: str,
        query: str,
        k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[Document]:
        """
        Semantic search over a collection (async).

        Args:
            collection: Collection name to search in.
            query: Natural language query string.
            k: Number of results to return.
            filters: Optional Qdrant filter dict (e.g. {"must": [...]}).

        Returns:
            List of matching Documents, ordered by relevance.
        """
        store = self.get_collection(collection)
        search_kwargs: dict[str, Any] = {"k": k}
        if filters:
            search_kwargs["filter"] = filters

        results: list[Document] = await store.asimilarity_search(query, **search_kwargs)
        logger.debug("Search '%s' in '%s' returned %d results", query, collection, len(results))
        return results

    def search_sync(
        self,
        collection: str,
        query: str,
        k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[Document]:
        """
        Semantic search over a collection (synchronous).

        Args:
            collection: Collection name to search in.
            query: Natural language query string.
            k: Number of results to return.
            filters: Optional Qdrant filter dict.

        Returns:
            List of matching Documents, ordered by relevance.
        """
        store = self.get_collection(collection)
        search_kwargs: dict[str, Any] = {"k": k}
        if filters:
            search_kwargs["filter"] = filters

        results: list[Document] = store.similarity_search(query, **search_kwargs)
        logger.debug("Search '%s' in '%s' returned %d results", query, collection, len(results))
        return results

    def delete_collection(self, name: str) -> None:
        """
        Delete a collection and its cached store.

        Args:
            name: Collection name to delete.
        """
        try:
            self._client.delete_collection(name)
            logger.info("Deleted collection '%s'", name)
        except (ResponseHandlingException, ValueError) as exc:
            logger.warning("Could not delete collection '%s': %s", name, exc)

        self._stores.pop(name, None)

    # ── Internal helpers ────────────────────────────────────────────

    def _ensure_collection(self, name: str) -> None:
        """Create the collection if it doesn't already exist."""
        try:
            self._client.get_collection(name)
            logger.debug("Collection '%s' already exists", name)
        except (ResponseHandlingException, UnexpectedResponse, ValueError):
            self._client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(
                    size=DEFAULT_VECTOR_SIZE,
                    distance=DEFAULT_DISTANCE,
                ),
            )
            logger.info(
                "Created collection '%s' (size=%d, distance=%s)",
                name,
                DEFAULT_VECTOR_SIZE,
                DEFAULT_DISTANCE,
            )

    def close(self) -> None:
        """Close the underlying Qdrant client connection."""
        self._client.close()
        self._stores.clear()
