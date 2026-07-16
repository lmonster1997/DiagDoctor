"""
bge-m3 embedding — dual backend: TEI (fast, async) → local sentence-transformers (fallback).

Priority:
1. TEI container (``tei_url``) — if reachable, use HTTP /embed
2. Local sentence-transformers — loads bge-m3 into memory (~2 GB, first call slow)

Both produce 1024-dim COSINE-compatible vectors from the same model.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

import httpx

from src.config import settings
from src.observability.logger import get_logger

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

logger = get_logger(__name__)

# Expected dimension for bge-m3
BGE_M3_DIM = 1024
BGE_M3_MODEL = "BAAI/bge-m3"

# ── Local model singleton (lazy load, cached across calls) ──────────

_local_model: SentenceTransformer | None = None
_local_model_lock = asyncio.Lock()


def _get_local_model() -> SentenceTransformer:
    """Load (or return cached) bge-m3 via sentence-transformers.

    Priority:
    1. ``BGE_M3_LOCAL_PATH`` env var — direct path to model directory
    2. HF Hub cache (``HF_HUB_CACHE`` / default ~/.cache/huggingface)
    """
    global _local_model
    if _local_model is None:
        from sentence_transformers import SentenceTransformer

        model_path = os.environ.get("BGE_M3_LOCAL_PATH", BGE_M3_MODEL)
        logger.info("loading_bge_m3", path=model_path)
        _local_model = SentenceTransformer(model_path)
        logger.info("bge_m3_loaded", device=str(_local_model.device))
    return _local_model


# ── Backend detection ────────────────────────────────────────────────


async def _tei_reachable() -> bool:
    """Check if TEI service is alive."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
            resp = await client.get(f"{settings.tei_url.rstrip('/')}/health")
            return resp.status_code == 200
    except Exception:
        return False


# ── Public API ───────────────────────────────────────────────────────


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts via bge-m3.

    Uses TEI if reachable, otherwise falls back to local sentence-transformers.
    Both produce identical 1024-dim vectors from the same BAAI/bge-m3 model.
    """
    if not texts:
        return []

    # ── Try TEI first ──
    if await _tei_reachable():
        url = f"{settings.tei_url.rstrip('/')}/embed"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                resp = await client.post(
                    url,
                    json={"inputs": texts, "truncate_dim": BGE_M3_DIM},
                )
                resp.raise_for_status()
                data = resp.json()
                embeddings: list[list[float]] = [d["embedding"] for d in data]
            logger.debug("tei_embed_success", count=len(embeddings), backend="tei")
            return embeddings
        except Exception:
            logger.warning("tei_embed_failed_falling_back", exc_info=True)

    # ── Fallback: local sentence-transformers ──
    async with _local_model_lock:
        model = _get_local_model()

    # sentence-transformers encode is sync; run in thread to avoid blocking
    loop = asyncio.get_running_loop()
    embeddings = await loop.run_in_executor(
        None,
        lambda: model.encode(
            texts,
            normalize_embeddings=True,  # COSINE-compatible
            show_progress_bar=False,
        ).tolist(),
    )
    logger.debug("local_embed_success", count=len(embeddings), backend="sentence-transformers")
    return embeddings


async def embed_single(text: str) -> list[float]:
    """Embed a single text. Convenience wrapper around ``embed_texts``."""
    results = await embed_texts([text])
    return results[0]
