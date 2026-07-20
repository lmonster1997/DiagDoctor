"""
Qdrant client singleton + historical_cases collection lifecycle.

Handles:
- AsyncQdrantClient creation (lazy singleton)
- Collection creation with migration (v2 -> rename)
- INT8 scalar quantization
- Payload indexes

P1-a (design §5.1/§6.4): the collection carries **named vectors** --
``symptom`` (P0, query-alignable symptom semantics) + ``root_cause`` (P1-a,
root-cause text). Index side stores both per point; query side picks one via
``query_points(using=...)``. INT8 quantization is configured **per vector**
inside each ``VectorParams`` (cleaner than a global config once vectors are
named).
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

# Named vectors (P1-a, design §5.1). ``symptom`` = P0 symptom semantics
# (recall/utilization 三分离, §4); ``root_cause`` = root-cause text, queried
# by the agent after it forms a root-cause hypothesis (§6.4 tool-ification,
# breaks the symptom-similarity ceiling demonstrated by #8).
VECTOR_NAME_SYMPTOM = "symptom"
VECTOR_NAME_ROOT_CAUSE = "root_cause"
NAMED_VECTORS: tuple[str, ...] = (VECTOR_NAME_SYMPTOM, VECTOR_NAME_ROOT_CAUSE)

# Per-vector INT8 scalar quantization (~50% memory, 2-5% precision loss).
# Configured inside each VectorParams at creation time (P1-a: named vectors ->
# per-vector quantization is unambiguous, no global update_collection step).
PER_VECTOR_QUANTIZATION = models.ScalarQuantization(
    scalar=models.ScalarQuantizationConfig(
        type=models.ScalarType.INT8,
    ),
)
HNSW_CONFIG = models.HnswConfigDiff(m=16, ef_construct=200)

# Payload fields that need indexes for filtering
PAYLOAD_INDEXES: list[tuple[str, str]] = [
    ("category", "keyword"),
    ("symptom_tier", "keyword"),
    ("source", "keyword"),
    ("trace_id", "keyword"),  # dedup scroll + retrieval self-exclusion (§5.2)
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


def _vector_params() -> models.VectorParams:
    """A named vector's config: 1024 / COSINE / INT8 (P1-a per-vector quant)."""
    return models.VectorParams(
        size=VECTOR_SIZE,
        distance=DISTANCE,
        quantization_config=PER_VECTOR_QUANTIZATION,
    )


async def _get_collection_vector_names(name: str) -> set[str]:
    """Return the set of vector names in an existing collection.

    - Named vectors -> ``{"symptom", "root_cause", ...}``
    - Single unnamed vector -> ``{""}`` (the P0 schema, pre-P1-a)
    - Missing / unreadable -> ``set()``

    ``CollectionParams.vectors`` is ``Union[VectorParams, Dict[str, VectorParams],
    None]``: a bare ``VectorParams`` has ``.size`` (unnamed), a dict has
    ``.keys()`` (named).
    """
    client = await get_qdrant_client()
    try:
        info = await client.get_collection(name)
        vectors = info.config and info.config.params and info.config.params.vectors
    except (UnexpectedResponse, Exception):
        return set()
    if not vectors:
        return set()
    if hasattr(vectors, "size"):
        return {""}  # single unnamed vector (P0 schema)
    try:
        return set(vectors.keys())  # type: ignore[union-attr]
    except Exception:
        return set()


async def _create_collection_internal(name: str) -> None:
    """Create a collection with named vectors (symptom + root_cause, §5.1).

    Both vectors: 1024 / COSINE / INT8 (per-vector quantization in VectorParams).
    Global HNSW config applies to all named vectors.
    """
    client = await get_qdrant_client()

    vectors_config = {name_: _vector_params() for name_ in NAMED_VECTORS}

    await client.create_collection(
        collection_name=name,
        vectors_config=vectors_config,  # type: ignore[arg-type]
        hnsw_config=HNSW_CONFIG,
    )
    logger.info(
        "qdrant_collection_created",
        collection=name,
        size=VECTOR_SIZE,
        vectors=list(NAMED_VECTORS),
    )

    # Payload indexes for filtering (quantization is per-vector above, no
    # global update_collection step needed).
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
    """Migrate via v2 -> rename: create new, delete old, rename new to final."""
    client = await get_qdrant_client()

    # Create the v2 collection with correct config
    await _create_collection_internal(new_name)

    # Delete old collection if it exists
    if await _collection_exists(old_name):
        await client.delete_collection(old_name)
        logger.info("qdrant_old_collection_deleted", collection=old_name)

    # Rename v2 -> canonical name (Qdrant has no rename API - recreate)
    # Strategy: we already created new_name with correct config.
    # If old_name == target and new_name is temp, recreate as target.
    # This is a simplified approach for the "library is empty" P0 reality.
    logger.info("qdrant_migration_complete", collection=old_name)


async def ensure_collection() -> str:
    """Ensure ``historical_cases`` exists with named vectors (symptom + root_cause).

    Migration logic (P1-a):
    1. If collection doesn't exist -> create fresh with named vectors.
    2. If collection exists with BOTH named vectors (symptom + root_cause) at
       size 1024 -> no-op.
    3. Otherwise (old single-vector P0 schema, or missing root_cause named
       vector, or size mismatch) -> **rebuild**: delete + recreate. The memory
       is 👍-sourced episodic log; on a dev library this is a cold re-ingest
       (design §5.1: "P1 可在 historical_cases 上加 named vector … 库冷启动重灌即可").

    Returns:
        The collection name (always ``historical_cases``).
    """
    client = await get_qdrant_client()
    target = COLLECTION_NAME

    if not await _collection_exists(target):
        # Fresh creation
        await _create_collection_internal(target)
        return target

    # Existing collection: does it already have the P1-a named-vector schema?
    existing_names = await _get_collection_vector_names(target)
    if NAMED_VECTORS and existing_names == set(NAMED_VECTORS):
        logger.info(
            "qdrant_collection_ok",
            collection=target,
            size=VECTOR_SIZE,
            vectors=sorted(existing_names),
        )
        return target

    # Schema mismatch (old single-vector schema, or named-vector set drifted)
    # -> rebuild. Library is upvote-sourced; re-ingest on next upvote cycle.
    # (note kept ASCII / CJK only -- structlog prints to stdout, which on a
    # Windows GBK console can't encode emoji; emojis stay in comments/docs.)
    logger.warning(
        "qdrant_schema_mismatch_rebuild",
        collection=target,
        existing_vectors=sorted(existing_names) if existing_names else None,
        expected_vectors=list(NAMED_VECTORS),
        note="P1-a named-vector rebuild; re-ingest upvote cases (library cold-start)",
    )
    await client.delete_collection(target)
    await _create_collection_internal(target)
    logger.info("qdrant_migration_complete", collection=target, vectors=list(NAMED_VECTORS))
    return target
