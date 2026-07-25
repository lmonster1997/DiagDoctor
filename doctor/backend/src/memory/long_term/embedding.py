"""
Embedding - primary DashScope API, legacy TEI/local bge-m3 fallback.

Priority:
1. DashScope (Alibaba) OpenAI-compatible API (``embedding_base_url`` +
   ``dashscope_api_key``) - when configured, used exclusively. No silent
   fallback to a different model: mixing embedders in one Qdrant collection
   would corrupt the vector space.
2. Legacy offline path (only when ``embedding_base_url`` is empty): TEI
   container (``tei_url``) -> local sentence-transformers (bge-m3).

API + legacy produce 1024-dim COSINE-compatible vectors (legacy from bge-m3;
API via ``dimensions=1024``). They are NOT interchangeable on the same
collection - config picks one deterministically.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

import httpx

from src.config import settings
from src.observability.logger import get_logger

if TYPE_CHECKING:
    from openai import AsyncOpenAI
    from sentence_transformers import SentenceTransformer

logger = get_logger(__name__)

# 默认离线运行:TEI 不可用时 fallback 到本地 bge-m3,联网校验/下载会被 SSL
# 拦且慢。setdefault 不覆盖用户显式设的值;TEI 路径不受影响(走 HTTP,不经
# transformers/huggingface_hub)。hf_hub_cache 从 settings 读后注入 os.environ
# (sentence-transformers 经 huggingface_hub 读这个 env 定位缓存根)。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
if settings.hf_hub_cache:
    os.environ.setdefault("HF_HUB_CACHE", settings.hf_hub_cache)

# Expected dimension for bge-m3
BGE_M3_DIM = 1024
BGE_M3_MODEL = "BAAI/bge-m3"

# ── Local model singleton (lazy load, cached across calls) ──────────

_local_model: SentenceTransformer | None = None
_local_model_lock = asyncio.Lock()


def _get_local_model() -> SentenceTransformer:
    """Load (or return cached) bge-m3 via sentence-transformers.

    Priority:
    1. ``bge_m3_local_path`` (settings) — direct path to model directory
    2. HF Hub cache (``HF_HUB_CACHE`` / default ~/.cache/huggingface)
    """
    global _local_model
    if _local_model is None:
        from sentence_transformers import SentenceTransformer

        model_path = settings.bge_m3_local_path or BGE_M3_MODEL
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


# ── DashScope API backend (primary when configured) ─────────────────

_api_client: AsyncOpenAI | None = None
_api_client_lock = asyncio.Lock()


async def _get_api_client() -> AsyncOpenAI:
    """Lazy singleton for the DashScope OpenAI-compatible client."""
    global _api_client
    if _api_client is None:
        async with _api_client_lock:
            if _api_client is None:
                from openai import AsyncOpenAI

                _api_client = AsyncOpenAI(
                    base_url=settings.embedding_base_url,
                    api_key=settings.dashscope_api_key.get_secret_value(),
                )
    return _api_client


async def _embed_via_api(texts: list[str]) -> list[list[float]]:
    """Embed via DashScope OpenAI-compatible /embeddings endpoint.

    No silent fallback: if the API fails the exception propagates (callers
    like ``case_store.maybe_index_diagnosis`` already try/except and skip).
    """
    client = await _get_api_client()
    resp = await client.embeddings.create(
        model=settings.embedding_model,
        input=texts,
        dimensions=settings.embedding_dimensions,
        encoding_format="float",
    )
    # OpenAI shape: resp.data is a list of {index, embedding}; sort by index
    # to guarantee alignment with the input order.
    ordered = sorted(resp.data, key=lambda d: d.index)
    embeddings = [d.embedding for d in ordered]
    logger.debug(
        "api_embed_success",
        count=len(embeddings),
        model=settings.embedding_model,
        dim=settings.embedding_dimensions,
    )
    return embeddings


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts.

    Routes to the DashScope API when ``embedding_base_url`` +
    ``dashscope_api_key`` are configured (primary); otherwise falls back to
    the legacy TEI / local bge-m3 path. The two paths use different models
    and are NOT interchangeable on the same Qdrant collection.
    """
    if not texts:
        return []

    # ── Primary: DashScope API (no fallback -- avoid mixing models) ──
    if settings.embedding_base_url and settings.dashscope_api_key.get_secret_value():
        return await _embed_via_api(texts)

    # ── Legacy offline path (embedding_base_url empty): TEI -> local bge-m3 ──
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
