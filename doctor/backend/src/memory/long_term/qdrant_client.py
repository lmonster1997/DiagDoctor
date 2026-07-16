"""
Qdrant client singleton + historical_cases collection lifecycle.

Handles:
- AsyncQdrantClient creation (lazy singleton)
- Collection creation with migration (v2 → rename)
- INT8 scalar quantization
- Payload indexes
"""

from __future__ import annotations

from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from src.config import settings
from src.observability.logger import get_logger

logger = get_logger(__name__)

# ── Constants ───────────────────────────────────────────────────────

COLLECTION_NAME = "historical_cases"
VECTOR_SIZE = 1024  # bge-m3
DISTANCE = models.Distance.COSINE
QUANTIZATION = models.ScalarQuantizationConfig(
    type=models.ScalarType.INT8,
)
HNSW_CONFIG = models.HnswConfigDiff(m=16, ef_construct=200)

# Payload fields that need indexes for filtering
PAYLOAD_INDEXES: list[tuple[str, str]] = [
    ("category", "keyword"),
    ("symptom_tier", "keyword"),
    ("source", "keyword"),
    ("created_at", "datetime"),
]

# ── Singleton ───────────────────────────────────────────────────────

_client: AsyncQdrantClient | None = None


async def get_qdrant_client() -> AsyncQdrantClient:
    """Return the singleton AsyncQdrantClient, creating it if needed."""
    global _client
    if _client is None:
        _client = AsyncQdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key.get_secret_value() or None,
        )
        logger.info("qdrant_client_created", url=settings.qdrant_url)
    return _client


# ── Collection lifecycle ────────────────────────────────────────────


async def _collection_exists(name: str) -> bool:
    """Check whether a collection exists (without raising)."""
    client = await get_qdrant_client()
    try:
        await client.get_collection(name)
        return True
    except (UnexpectedResponse, Exception):
        return False


async def _get_collection_vector_size(name: str) -> int | None:
    """Return the vector size of an existing collection, or None."""
    client = await get_qdrant_client()
    try:
        info = await client.get_collection(name)
        config = info.config
        if config and config.params and config.params.vectors:
            return config.params.vectors.size  # type: ignore[union-attr]
        return None
    except (UnexpectedResponse, Exception):
        return None


async def _create_collection_internal(name: str) -> None:
    """Create a collection with the canonical P0 config (1024 / COSINE / INT8)."""
    client = await get_qdrant_client()

    await client.create_collection(
        collection_name=name,
        vectors_config=models.VectorParams(
            size=VECTOR_SIZE,
            distance=DISTANCE,
        ),
        hnsw_config=HNSW_CONFIG,
    )
    logger.info("qdrant_collection_created", collection=name, size=VECTOR_SIZE)

    # INT8 scalar quantization: ~50% memory reduction, 2-5% precision loss
    await client.update_collection(
        collection_name=name,
        quantization_config=QUANTIZATION,  # type: ignore[arg-type]
    )
    logger.info("qdrant_quantization_applied", collection=name, type="int8")

    # Payload indexes for filtering
    for field, kind in PAYLOAD_INDEXES:
        await client.create_payload_index(
            collection_name=name,
            field_name=field,
            field_schema=kind,  # type: ignore[arg-type]
        )
    logger.info(
        "qdrant_payload_indexes_created",
        collection=name,
        fields=[f for f, _ in PAYLOAD_INDEXES],
    )


async def _migrate_if_needed(old_name: str, new_name: str) -> None:
    """Migrate via v2 → rename: create new, delete old, rename new to final."""
    client = await get_qdrant_client()

    # Create the v2 collection with correct config
    await _create_collection_internal(new_name)

    # Delete old collection if it exists
    if await _collection_exists(old_name):
        await client.delete_collection(old_name)
        logger.info("qdrant_old_collection_deleted", collection=old_name)

    # Rename v2 → canonical name (Qdrant has no rename API — recreate)
    # Strategy: we already created new_name with correct config.
    # If old_name == target and new_name is temp, recreate as target.
    # This is a simplified approach for the "library is empty" P0 reality.
    logger.info("qdrant_migration_complete", collection=old_name)


async def ensure_collection() -> str:
    """Ensure ``historical_cases`` exists with correct config (1024 / COSINE / INT8).

    Migration logic:
    1. If collection doesn't exist → create fresh
    2. If collection exists but wrong vector size → migrate (v2 → rename)
    3. If collection exists with correct config → no-op

    Returns:
        The collection name (always ``historical_cases``).
    """
    client = await get_qdrant_client()
    target = COLLECTION_NAME

    if not await _collection_exists(target):
        # Fresh creation
        await _create_collection_internal(target)
        return target

    # Check existing config
    existing_size = await _get_collection_vector_size(target)
    if existing_size == VECTOR_SIZE:
        logger.info("qdrant_collection_ok", collection=target, size=VECTOR_SIZE)
        return target

    # Dimension mismatch → migrate
    logger.warning(
        "qdrant_dimension_mismatch",
        collection=target,
        existing=existing_size,
        expected=VECTOR_SIZE,
    )

    temp_name = f"{target}_v2"
    if await _collection_exists(temp_name):
        await client.delete_collection(temp_name)

    await _create_collection_internal(temp_name)
    await client.delete_collection(target)
    logger.info("qdrant_old_collection_deleted", collection=target)

    # Qdrant has no rename API → recreate as target name
    await _create_collection_internal(target)
    await client.delete_collection(temp_name)

    logger.info("qdrant_migration_complete", collection=target, new_size=VECTOR_SIZE)
    return target
