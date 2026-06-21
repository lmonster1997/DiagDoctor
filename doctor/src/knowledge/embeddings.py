"""
Embedding model loading with fallback strategy.

Priority:
1. OpenAI-compatible API (from config: EMBEDDING_BASE_URL, EMBEDDING_MODEL, LLM_API_KEY)
2. Local sentence-transformers (bge-m3)

Provides a unified factory: get_embeddings() -> Embeddings.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.embeddings import Embeddings

from src.config import settings

logger = logging.getLogger(__name__)

_embeddings_cache: Embeddings | None = None


def _create_openai_embeddings() -> Embeddings:
    """Create OpenAI-compatible embeddings from config."""
    from langchain_openai import OpenAIEmbeddings

    base_url = settings.embedding_base_url or settings.llm_base_url
    api_key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else ""

    logger.info(
        "Using OpenAI-compatible embeddings: model=%s, base_url=%s",
        settings.embedding_model,
        base_url,
    )

    kwargs: dict[str, Any] = {
        "model": settings.embedding_model,
        "openai_api_key": api_key,
    }
    if settings.embedding_base_url:
        kwargs["openai_api_base"] = settings.embedding_base_url
    else:
        kwargs["openai_api_base"] = settings.llm_base_url

    return OpenAIEmbeddings(**kwargs)


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
