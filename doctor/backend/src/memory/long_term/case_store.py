"""
Historical case store - index 👍-approved diagnosis reports into Qdrant.

P0 scope:
- ``maybe_index_diagnosis()``: validate -> embed -> upsert, async fire-and-forget
- ``backfill_effectiveness()``: 👍/👎 write-back of ``effectiveness`` / ``hit_count``
  on the cases recalled during a diagnosis (§8.1) -- closes the "越用越准" loop
  that ``maybe_index_diagnosis`` alone leaves open.
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

SOURCE_USER_UPVOTE = "user_upvote"
# Retrieval threshold (RELEVANCE_THRESHOLD) lives in case_retriever.py (calibration TODO §9.1).


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
    source: str,
    case_id: str,
    trace_id: str,
) -> PointStruct:
    """Construct a Qdrant PointStruct with indexed payload + named vectors.

    P1-a (design §5.1/§6.4): the point carries two named vectors --
    ``symptom`` (P0, query-alignable symptom semantics) and ``root_cause``
    (P1-a, root-cause text). The query side picks one via
    ``query_points(using=...)``; symptom breaks the symptom-similarity ceiling.
    """
    now = datetime.now(UTC).isoformat()

    return PointStruct(
        id=case_id,  # UUID -> upsert 天然幂等
        vector={
            VECTOR_NAME_SYMPTOM: symptom_vector,
            VECTOR_NAME_ROOT_CAUSE: root_cause_vector,
        },
        payload={
            # ── 去重 / 溯源 ──
            "trace_id": trace_id,
            "case_id": case_id,
            # ── 结构化锚 (filter / rerank / injection label, §5.2) ──
            "category": report.primary_category,
            "symptom_tier": report.symptom_tier,
            "is_cross_layer": bool(evidence.correlations),
            "root_cause_tier": report.root_cause_tier,
            "signal_types": [s.signal_type for s in evidence.golden_signals],
            "affected_files": _resolve_affected_files(report),
            # ── 诊断输出 (injection 利用,全文不截断,§5.2) ──
            "root_cause": report.root_cause,
            "fix_suggestion": report.fix_suggestion,
            "confidence": report.confidence,
            "user_report_snippet": evidence.user_report[:200],
            # ── 治理字段 (three-factor importance / feedback loop, §6.1/§8) ──
            "hit_count": 0,  # 检索命中次数 (feedback loop 写入)
            "effectiveness": 0.0,  # 回流有效性分 (feedback loop 写入)
            # ── 元数据 ──
            "source": source,
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
    source: str = SOURCE_USER_UPVOTE,
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
        source: Always ``"user_upvote"`` in P0 - kept as param for P1 auto channel.
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
        source,
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
# Feedback backfill (design §8.1) - close the "越用越准" loop
# ═════════════════════════════════════════════════════════════════════
# #1 captures the case_ids recalled during diagnosis into ``DoctorState``
# (``retrieved_case_ids``), but indexing the *new* case on 👍 alone doesn't
# touch those recalled cases -- so ``_importance`` (case_retriever) keeps
# reading ``hit_count`` / ``effectiveness`` = 0 and the loop never tightens.
# This function is the write-back half: 👍 the diagnosis -> the cases that
# were recalled (and helped) get credited.


async def backfill_effectiveness(
    case_ids: list[str],
    *,
    delta: float,
    hit: bool = True,
) -> int:
    """Backfill ``effectiveness`` / ``hit_count`` on recalled cases (§8.1).

    - 👍 (``hit=True``, ``delta>0``): the recalled cases helped reach a
      diagnosis the user endorsed -> ``effectiveness += delta`` (clamped to
      ``[0, 1]``) and ``hit_count += 1``.
    - 👎 (``hit=False``, ``delta<0``): ``effectiveness`` is decremented
      (clamped); ``hit_count`` is left unchanged -- a 👎 is not a confirming
      hit (it's still a *retrieval* hit, but ``hit_count`` here is the
      "useful retrieval" counter the importance formula rewards).

    Qdrant ``set_payload`` overwrites with a literal value (no native
    increment), so this is a read-modify-write: retrieve current payloads by
    point id (``case_id == point id`` by design §3.10), compute the new
    values, then ``set_payload`` per point. A ``case_id`` no longer present
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
        old_hits = int(payload.get("hit_count", 0) or 0)
        new_eff = max(0.0, min(1.0, old_eff + delta))
        new_hits = old_hits + 1 if hit else old_hits
        try:
            await client.set_payload(
                collection_name=COLLECTION_NAME,
                payload={"effectiveness": new_eff, "hit_count": new_hits},
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
        hit=hit,
    )
    return updated
