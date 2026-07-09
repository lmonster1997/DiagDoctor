"""
Embedding model loading with fallback strategy.

Priority:
1. OpenAI-compatible API (from config: EMBEDDING_BASE_URL, EMBEDDING_MODEL, LLM_API_KEY)
2. Local sentence-transformers (bge-m3)

Provides a unified factory: get_embeddings() -> Embeddings.
"""

from __future__ import annotations

import logging

from langchain_core.embeddings import Embeddings

from src.config import settings

logger = logging.getLogger(__name__)

_embeddings_cache: Embeddings | None = None


class _DashScopeEmbeddings(Embeddings):
    """Minimal OpenAI-compatible embeddings wrapper.

    Uses the raw ``openai`` client directly, bypassing LangChain's
    ``OpenAIEmbeddings`` tokenization layer.  LangChain tokenizes text
    into token-IDs before sending (e.g. ``[[15339]]``), which DashScope's
    compatible API rejects with ``InvalidParameter: input.contents``.

    This class sends plain text strings, matching exactly what the
    vanilla ``openai`` client does.
    """

    def __init__(self, model: str, api_key: str, base_url: str) -> None:
        import httpx

        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=httpx.Timeout(30.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of documents."""
        result: list[list[float]] = []
        for i in range(0, len(texts), 20):  # batch ≤ 20
            batch = texts[i : i + 20]
            resp = self._client.post(
                f"{self._base_url}/embeddings",
                json={"model": self._model, "input": batch},
            )
            resp.raise_for_status()
            data = resp.json()
            result.extend(item["embedding"] for item in data["data"])
        return result

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query."""
        return self.embed_documents([text])[0]


def _create_openai_embeddings() -> Embeddings:
    """Create OpenAI-compatible embeddings from config.

    Uses the lightweight :class:`_DashScopeEmbeddings` wrapper that sends
    plain text instead of token-IDs, so it works with DashScope and any
    other OpenAI-compatible provider that expects raw strings.
    """
    base_url = settings.embedding_base_url or settings.llm_base_url
    api_key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else ""

    logger.info(
        "Using DashScope-compatible embeddings: model=%s, base_url=%s",
        settings.embedding_model,
        base_url,
    )

    return _DashScopeEmbeddings(
        model=settings.embedding_model,
        api_key=api_key,
        base_url=base_url,
    )


def _create_local_embeddings() -> Embeddings:
    """Create local sentence-transformers embeddings (bge-m3 fallback)."""
    from langchain_community.embeddings import HuggingFaceEmbeddings

    logger.info("Using local sentence-transformers embeddings: model=BAAI/bge-m3")

    return HuggingFaceEmbeddings(
        model_name="BAAI/bge-m3",
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
    )


def get_embeddings() -> Embeddings:
    """
    Get the embeddings instance (cached).

    Tries OpenAI-compatible API first; falls back to local bge-m3 if API key
    is not configured or if the API call fails at init time.
    """
    global _embeddings_cache

    if _embeddings_cache is not None:
        return _embeddings_cache

    # Try OpenAI-compatible first
    api_key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else ""
    if api_key:
        try:
            _embeddings_cache = _create_openai_embeddings()
            return _embeddings_cache
        except Exception as exc:
            logger.warning(
                "Failed to create OpenAI embeddings, falling back to local: %s",
                exc,
            )

    # Fallback to local
    _embeddings_cache = _create_local_embeddings()
    return _embeddings_cache


def reset_embeddings_cache() -> None:
    """Clear the embeddings cache (useful for testing)."""
    global _embeddings_cache
    _embeddings_cache = None
