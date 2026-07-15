"""
Historical case store — index 👍-approved diagnosis reports into Qdrant.

P0 scope:
- ``maybe_index_diagnosis()``: validate → embed → upsert, async fire-and-forget
- ``_build_passage_text()``: construct embedding passage
  (metadata anchor + user_report + root_cause + fix_suggestion)
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
from src.memory.long_term.qdrant_client import (
    COLLECTION_NAME,
    get_qdrant_client,
)
from src.observability.logger import get_logger

logger = get_logger(__name__)

# ── Constants ───────────────────────────────────────────────────────

SOURCE_USER_UPVOTE = "user_upvote"
SIMILARITY_THRESHOLD = 0.75  # minimum score for retrieved cases


# ═════════════════════════════════════════════════════════════════════
# Passage construction
# ═════════════════════════════════════════════════════════════════════


def _build_passage_text(report: DiagnosisReport, evidence: NormalizedEvidence) -> str:
    """Construct the embedding passage from report + evidence.

    Structure: [诊断元数据] ... (user_report) ... (root_cause) ... (fix_suggestion)

    Metadata anchor goes FIRST for bge-m3 positional bias.
    fix_suggestion is kept full (no truncation) — code fragments like
    field names and function signatures must not be lost.
    """
    signal_types = sorted({s.signal_type for s in evidence.golden_signals})
    tier = "cross_layer" if evidence.correlations else report.symptom_tier

    meta = (
        f"[诊断元数据] 信号类型: {', '.join(signal_types) if signal_types else '未识别'} "
        f"| 类别: {report.primary_category} "
        f"| 层级: {tier} "
        f"| 涉及文件: {report.affected_file or '未定位'}"
    )

    parts: list[str] = [meta]

    if evidence.user_report:
        parts.append(evidence.user_report)

    if report.root_cause:
        parts.append(report.root_cause)

    if report.fix_suggestion:
        parts.append(report.fix_suggestion)

    return "\n\n".join(parts)


def _build_query_text(evidence: NormalizedEvidence) -> str:
    """Construct the query-side passage (symmetric to index-side, minus root_cause/fix)."""
    signal_types = sorted({s.signal_type for s in evidence.golden_signals})
    tier = "cross_layer" if evidence.correlations else "backend"

    meta = (
        f"[诊断元数据] 信号类型: {', '.join(signal_types) if signal_types else '未识别'} "
        f"| 层级: {tier}"
    )

    parts: list[str] = [meta]
    if evidence.user_report:
        parts.append(evidence.user_report)

    return "\n\n".join(parts)


# ═════════════════════════════════════════════════════════════════════
# Qdrant point construction
# ═════════════════════════════════════════════════════════════════════


def _resolve_affected_files(report: DiagnosisReport) -> list[str]:
    """Normalise affected files into a list — report.affected_file is singular."""
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
        id=case_id,  # UUID → upsert 天然幂等
        vector=vector,
        payload={
            "trace_id": trace_id,
            "case_id": case_id,
            "category": report.primary_category,
            "symptom_tier": report.symptom_tier,
            "signal_types": [s.signal_type for s in evidence.golden_signals],
            "affected_files": _resolve_affected_files(report),
            "root_cause": report.root_cause,
            "confidence": report.confidence,
            "source": source,
            "created_at": now,
            "user_report_snippet": evidence.user_report[:200],
            "fix_snippet": report.fix_suggestion[:300],
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
        source: Always ``"user_upvote"`` in P0 — kept as param for P1 auto channel.
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
    passage = _build_passage_text(report, evidence)
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
