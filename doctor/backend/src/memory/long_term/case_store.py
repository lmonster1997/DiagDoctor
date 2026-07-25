"""
Historical case store - index 👍-approved diagnosis reports into Qdrant.

P0 scope:
- ``maybe_index_diagnosis()``: validate -> embed -> upsert, async fire-and-forget
- ``backfill_effectiveness()``: case-level "有帮助" write-back of ``effectiveness``
  on endorsed cases (§8.2) -- closes the "越用越准" loop that
  ``maybe_index_diagnosis`` alone leaves open.
- ``build_symptom_passage()`` (from ``encoding``): the embedding passage --
  symptom-only, shared with the query side (recall/utilization 三分离, §4)
- ``_dedup_exists()``: check trace_id duplicates (warn, don't reject)
- ``_build_point()``: construct Qdrant PointStruct with full payload

P1-a (design §5.1/§6.4): ``maybe_index_diagnosis`` embeds BOTH the symptom
passage and the root_cause text into two named vectors per point
(``symptom`` + ``root_cause``). The query side (``case_retriever``) picks one
via ``query_points(using=...)``; the ``root_cause`` vector is what the agent's
root-cause-hypothesis tool queries to break the symptom-similarity ceiling (#8).

P0 does NOT include: auto-silence channel, failed-case collection, llm_judge gating.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from qdrant_client.models import PointStruct

from src.engine.state import DiagnosisReport, NormalizedEvidence
from src.memory.long_term.embedding import embed_texts
from src.memory.long_term.encoding import build_symptom_passage
from src.memory.long_term.qdrant_client import (
    COLLECTION_NAME,
    VECTOR_NAME_ROOT_CAUSE,
    VECTOR_NAME_SYMPTOM,
    get_qdrant_client,
)
from src.observability.logger import get_logger

logger = get_logger(__name__)

# ── Constants ───────────────────────────────────────────────────────

# Retrieval thresholds (SYMPTOM_/ROOT_CAUSE_RELEVANCE_THRESHOLD) live in
# case_retriever.py (calibration TODO §9.1, two separate per-layer thresholds).
# ``source`` (user_upvote vs expert_curated) was dropped from the payload: the
# expert channel is explicitly out of scope (design §5.2/§10), so source is
# constant and carries no filter/dispatch signal.


# ═════════════════════════════════════════════════════════════════════
# Passage construction
# ═════════════════════════════════════════════════════════════════════
# Embedding passage construction lives in ``encoding.build_symptom_passage`` --
# shared between index side (here) and query side (``case_retriever``) so the
# two vectors are truly symmetric (recall/utilization 三分离, design §4):
# the vector carries symptoms only; root_cause / fix / category /
# affected_files stay in the payload (see ``_build_point`` below).


# ═════════════════════════════════════════════════════════════════════
# Qdrant point construction
# ═════════════════════════════════════════════════════════════════════


def _resolve_affected_files(report: DiagnosisReport) -> list[str]:
    """Normalise affected files into a list - report.affected_file is singular."""
    files: list[str] = []
    if report.affected_file:
        files.append(report.affected_file)
    return files


def _build_point(
    report: DiagnosisReport,
    evidence: NormalizedEvidence,
    symptom_vector: list[float],
    root_cause_vector: list[float],
    case_id: str,
    trace_id: str,
) -> PointStruct:
    """Construct a Qdrant PointStruct with indexed payload + named vectors.

    P1-a (design §5.1/§6.4): the point carries two named vectors --
    ``symptom`` (P0, query-alignable symptom semantics) and ``root_cause``
    (P1-a, root-cause text). The query side picks one via
    ``query_points(using=...)``; symptom breaks the symptom-similarity ceiling.

    Payload is trimmed to 9 core fields (design §5.2): identity
    (``case_id``/``trace_id``), injection content (``root_cause``/
    ``fix_suggestion``/``user_report_snippet``/``affected_files``), and
    scoring inputs (``confidence``/``effectiveness``/``created_at``).
    Display-only / dead fields were dropped: ``category``/``source``
    (no filter/dispatch, root_cause text already carries category),
    ``is_cross_layer``/``symptom_tier``/``root_cause_tier`` (tier filter
    reversed, §附录 B), ``signal_types`` (filter never wired), ``hit_count``
    (mathematically redundant with ``effectiveness``, §6.1).
    ``derive_tier`` is retained in ``encoding`` for eval/unit tests but is no
    longer stored in the payload.
    """
    now = datetime.now(UTC).isoformat()

    return PointStruct(
        id=case_id,  # UUID -> upsert 天然幂等
        vector={
            VECTOR_NAME_SYMPTOM: symptom_vector,
            VECTOR_NAME_ROOT_CAUSE: root_cause_vector,
        },
        payload={
            # ── 身份 / 溯源 ──
            "case_id": case_id,
            "trace_id": trace_id,
            # ── 注入内容 (injection 利用,全文不截断,§5.2) ──
            "root_cause": report.root_cause,
            "fix_suggestion": report.fix_suggestion,
            "user_report_snippet": evidence.user_report[:200],
            "affected_files": _resolve_affected_files(report),
            # ── 治理字段 (three-factor importance / feedback loop, §6.1/§8) ──
            "confidence": report.confidence,
            "effectiveness": 0.0,  # 认可关联分 (case 级"有帮助"写入,§8.2)
            "created_at": now,
        },
    )


# ═════════════════════════════════════════════════════════════════════
# Dedup helpers
# ═════════════════════════════════════════════════════════════════════


async def _dedup_exists(*, trace_id: str) -> bool:
    """Check whether a case with the given trace_id already exists.

    Uses Qdrant scroll with payload filter (keyword index on ``trace_id``).
    Returns True if at least one point with that trace_id exists.
    """
    if not trace_id:
        return False

    from qdrant_client import models

    client = await get_qdrant_client()

    try:
        result = await client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="trace_id",
                        match=models.MatchValue(value=trace_id),
                    )
                ]
            ),
            limit=1,
            with_payload=False,
        )
        return len(result[0]) > 0
    except Exception:
        logger.warning("dedup_check_failed", exc_info=True)
        return False


# ═════════════════════════════════════════════════════════════════════
# Main entry point
# ═════════════════════════════════════════════════════════════════════


async def maybe_index_diagnosis(
    report: DiagnosisReport,
    evidence: NormalizedEvidence,
    *,
    trace_id: str = "",
    case_id: str | None = None,
) -> bool:
    """Index a diagnosis report into Qdrant if it passes hard guards.

    Hard guards (P0):
    1. Report completeness: ``root_cause`` + ``affected_file`` + ``fix_suggestion`` all non-empty
    2. Dedup warning: log if same ``trace_id`` already exists (but do NOT reject)

    Args:
        report: The final DiagnosisReport from the agent.
        evidence: The NormalizedEvidence that went into diagnosis.
        trace_id: The W3C trace_id associated with this bug trigger.
        case_id: Point ID in Qdrant (UUID). Auto-generated if not provided.

    Returns:
        True if indexed, False if skipped (hard guard rejection).
    """
    # ── Hard guard 1: report completeness ──
    if not (report.root_cause and report.affected_file and report.fix_suggestion):
        logger.info(
            "index_skipped_incomplete_report",
            has_root_cause=bool(report.root_cause),
            has_affected_file=bool(report.affected_file),
            has_fix=bool(report.fix_suggestion),
        )
        return False

    # ── Hard guard 2: dedup warning (not rejection) ──
    if trace_id and await _dedup_exists(trace_id=trace_id):
        logger.info("duplicate_trace_id_upvote", trace_id=trace_id)

    # ── Build passages & embed both vectors (single batch call) ──
    # symptom passage (shared with the query side); root_cause text (P1-a).
    # Both are root-cause/symptom semantics only -- diagnosis outputs stay in
    # the payload, NOT in the vectors (recall/utilization 三分离, §4 + §5.1).
    # Batched so a single embed round-trip produces both; if it fails, the
    # case is skipped (mirrors P0 -- RAG indexing is a gain, not a dependency).
    symptom_passage = build_symptom_passage(evidence)
    root_cause_text = report.root_cause
    try:
        vectors = await embed_texts([symptom_passage, root_cause_text])
        symptom_vector, root_cause_vector = vectors[0], vectors[1]
    except Exception:
        logger.error("embedding_failed_during_index", exc_info=True)
        return False

    # ── Build point & upsert ──
    resolved_case_id = case_id or str(uuid.uuid4())
    point = _build_point(
        report,
        evidence,
        symptom_vector,
        root_cause_vector,
        resolved_case_id,
        trace_id,
    )

    try:
        client = await get_qdrant_client()
        await client.upsert(collection_name=COLLECTION_NAME, points=[point])
        logger.info(
            "historical_case_indexed",
            case_id=resolved_case_id,
            trace_id=trace_id,
            category=report.primary_category,
        )
        return True
    except Exception:
        logger.error("qdrant_upsert_failed", exc_info=True)
        return False


# ═════════════════════════════════════════════════════════════════════
# Feedback backfill (design §8.2) - close the "越用越准" loop
# ═════════════════════════════════════════════════════════════════════
# The case-level "有帮助" endpoint (§8.1 path 2) credits ``effectiveness`` on
# the specific cases the agent referenced AND the user endorsed. Indexing a
# new case on 👍 alone doesn't touch those referenced cases -- this is the
# write-back half. ``effectiveness`` is a "认可关联分" (acknowledged-affinity
# score, §8.2): monotonic up only -- "没帮助"/👎 does NOT decrement
# (attribution ambiguous), so ``delta`` is always >= 0 in practice.


async def backfill_effectiveness(
    case_ids: list[str],
    *,
    delta: float,
) -> int:
    """Backfill ``effectiveness`` on endorsed cases (§8.2 认可关联分).

    ``effectiveness += delta`` (clamped to ``[0, 1]``). Called with a positive
    ``delta`` only when a user marks a referenced case "有帮助" (§8.1 path 2) --
    "没帮助"/no-action does NOT call this (只升不降, §8.2). The clamp is
    defensive (lower bound is trivially satisfied since delta >= 0).

    Qdrant ``set_payload`` overwrites with a literal value (no native
    increment), so this is a read-modify-write: retrieve current payloads by
    point id (``case_id == point id`` by design §3.10), compute the new value,
    then ``set_payload`` per point. A ``case_id`` no longer present
    (deleted / never indexed) is silently skipped -- ``retrieve`` just omits
    it from the result.

    Returns the number of points actually updated. Any failure degrades to a
    logged warning and returns 0 (or a partial count): feedback is a gain,
    not a dependency -- mirrors ``case_retriever`` / ``maybe_index_diagnosis``.
    """
    if not case_ids:
        return 0

    try:
        client = await get_qdrant_client()
        records = await client.retrieve(
            collection_name=COLLECTION_NAME,
            ids=case_ids,
            with_payload=True,
            with_vectors=False,
        )
    except Exception:
        logger.warning("backfill_retrieve_failed", case_ids=case_ids, exc_info=True)
        return 0

    updated = 0
    for record in records:
        payload = dict(record.payload or {})
        old_eff = float(payload.get("effectiveness", 0.0) or 0.0)
        new_eff = max(0.0, min(1.0, old_eff + delta))
        try:
            await client.set_payload(
                collection_name=COLLECTION_NAME,
                payload={"effectiveness": new_eff},
                points=[record.id],
            )
            updated += 1
        except Exception:
            logger.warning("backfill_set_payload_failed", point_id=str(record.id), exc_info=True)

    logger.info(
        "backfill_effectiveness_done",
        requested=len(case_ids),
        found=len(records),
        updated=updated,
        delta=delta,
    )
    return updated
