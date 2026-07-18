"""
Historical case store - index 👍-approved diagnosis reports into Qdrant.

P0 scope:
- ``maybe_index_diagnosis()``: validate -> embed -> upsert, async fire-and-forget
- ``build_symptom_passage()`` (from ``encoding``): the embedding passage --
  symptom-only, shared with the query side (recall/utilization 三分离, §4)
- ``_dedup_exists()``: check trace_id duplicates (warn, don't reject)
- ``_build_point()``: construct Qdrant PointStruct with full payload

P0 does NOT include: auto-silence channel, failed-case collection, llm_judge gating.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from qdrant_client.models import PointStruct

from src.engine.state import DiagnosisReport, NormalizedEvidence
from src.memory.long_term.embedding import embed_single
from src.memory.long_term.encoding import build_symptom_passage
from src.memory.long_term.qdrant_client import (
    COLLECTION_NAME,
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
    vector: list[float],
    source: str,
    case_id: str,
    trace_id: str,
) -> PointStruct:
    """Construct a Qdrant PointStruct with indexed payload."""
    now = datetime.now(UTC).isoformat()

    return PointStruct(
        id=case_id,  # UUID -> upsert 天然幂等
        vector=vector,
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

    # ── Build passage & embed ──
    # Symptom-only passage (shared with the query side); root_cause / fix
    # stay in the payload, NOT in the vector (recall/utilization 三分离, §4).
    passage = build_symptom_passage(evidence)
    try:
        vector = await embed_single(passage)
    except Exception:
        logger.error("embedding_failed_during_index", exc_info=True)
        return False

    # ── Build point & upsert ──
    resolved_case_id = case_id or str(uuid.uuid4())
    point = _build_point(report, evidence, vector, source, resolved_case_id, trace_id)

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
