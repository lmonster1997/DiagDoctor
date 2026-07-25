"""
Feedback API — user 👍/👎 on diagnosis reports.

P0 scope:
- ``POST /api/feedback/{run_id}/upvote`` → async index diagnosis into Qdrant
  + §8.1 backfill: credit ``effectiveness`` on the recalled cases
- POST /api/feedback/{run_id}/downvote -> structured log only (no backfill:
  👎 attribution is ambiguous, downgrading recalled cases would penalize good
  cases -- see design §8.1/§8.2). P1: failure-pattern mining input.

The ``run_id`` is the LangGraph ``thread_id``, which maps to the checkpoint
state that holds ``report`` (DiagnosisReport) and ``evidence`` (NormalizedEvidence).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.engine.state import DiagnosisReport, NormalizedEvidence
from src.observability.logger import get_logger

router = APIRouter(prefix="/api/feedback", tags=["feedback"])
logger = get_logger(__name__)

# ── §8.1 effectiveness backfill policy ──────────────────────────────
# The *mechanism* (read-modify-write into Qdrant) lives in
# ``case_store.backfill_effectiveness``; the *policy* -- how much a single
# case-level "有帮助" mark moves a referenced case's effectiveness -- lives
# here, next to the trigger. +0.1 per helpful mark (design §8.1: "如 +0.1,
# 上限 1.0"); effectiveness is clamped to [0, 1] by the mechanism.
#
# Only the case-level endpoint (path 2) backfills -- 👍 no longer touches
# recalled cases (task 3c: 👍 endorses the *diagnosis*, crediting all recalled
# cases was coarse attribution; 👍 now only indexes the new case). 👎 /
# "没帮助" do NOT backfill (attribution ambiguous -- see design §8.1/§8.2,
# 只升不降).
EFFECTIVENESS_HELPFUL_DELTA = 0.1


# ── Internal helpers ────────────────────────────────────────────────


async def _load_run_state(
    run_id: str,
) -> tuple[DiagnosisReport | None, NormalizedEvidence | None, str, list[str]]:
    """Load DiagnosisReport + NormalizedEvidence + recalled case_ids from the
    LangGraph checkpoint.

    The MemorySaver checkpointer stores graph state keyed by ``thread_id``
    (which equals ``run_id``).  We fetch the final state snapshot.

    Returns:
        ``(report, evidence, trace_id, retrieved_case_ids)`` — any may be
        None/empty if not found. ``retrieved_case_ids`` is the list of
        historical case_ids recalled during this diagnosis (captured by #1 on
        pass 1) — consumed by the §8.1 backfill on 👍/👎.
    """
    from src.engine.nodes.diagnosis_agent import get_copilotkit_graph

    graph = get_copilotkit_graph()
    config = {"configurable": {"thread_id": run_id}}

    try:
        snapshot = await graph.aget_state(config)
    except Exception:
        logger.warning("feedback_load_state_failed", run_id=run_id, exc_info=True)
        return None, None, "", []

    if snapshot is None or snapshot.values is None:
        return None, None, "", []

    state: dict[str, Any] = snapshot.values
    report = state.get("report")
    evidence = state.get("evidence")
    retrieved_case_ids = list(state.get("retrieved_case_ids") or [])

    # Resolve trace_id: first from trigger_trace_ids, then metadata
    trace_id = ""
    if evidence is not None and isinstance(evidence, NormalizedEvidence):
        if evidence.trigger_trace_ids:
            trace_id = evidence.trigger_trace_ids[0]
        elif evidence.metadata:
            trace_id = evidence.metadata.get("trace_id", "")

    return report, evidence, trace_id, retrieved_case_ids


# ═════════════════════════════════════════════════════════════════════
# Routes
# ═════════════════════════════════════════════════════════════════════


@router.post("/{run_id}/upvote", status_code=200)
async def upvote(run_id: str) -> dict[str, object]:
    """User 👍: index the diagnosis report into historical_cases (Qdrant).

    This is the **only** P0 indexing trigger.  The write is async
    (``asyncio.create_task``) — the HTTP response returns immediately.
    """
    report, evidence, trace_id, _retrieved_case_ids = await _load_run_state(run_id)

    if report is None:
        raise HTTPException(
            status_code=404,
            detail=f"No diagnosis report found for run_id={run_id}. "
            "The diagnosis may not have completed yet, or the checkpoint may have expired.",
        )

    if evidence is None:
        raise HTTPException(
            status_code=422,
            detail="Diagnosis report exists but no evidence found — cannot index without evidence.",
        )

    # Fire-and-forget: don't block the HTTP response on Qdrant I/O
    async def _index() -> None:
        from src.memory.long_term.case_store import maybe_index_diagnosis

        try:
            indexed = await maybe_index_diagnosis(
                report=report,
                evidence=evidence,
                trace_id=trace_id,
                case_id=run_id,  # use thread_id as point id for idempotency
            )
            if indexed:
                logger.info("upvote_indexed", run_id=run_id, trace_id=trace_id)
            else:
                logger.info("upvote_skipped", run_id=run_id, reason="hard_guard")
        except Exception:
            logger.error("upvote_index_failed", run_id=run_id, exc_info=True)

    asyncio.create_task(_index())

    return {"ok": True, "run_id": run_id}


@router.post("/{run_id}/downvote", status_code=200)
async def downvote(run_id: str) -> dict[str, object]:
    """User 👎: log structured feedback only -- no effectiveness backfill.

    A 👎 means the diagnosis was wrong, but the failure attribution is
    ambiguous (the recalled cases may be correct-but-inapplicable, the agent
    may have reasoned wrong, or the root cause may be a coverage gap).
    Downgrading the recalled cases would penalize good cases, so we do NOT
    backfill effectiveness on 👎 (design §8.1/§8.2). The structured log is
    kept as input for future P1-b failure-pattern mining.
    """
    report, evidence, trace_id, retrieved_case_ids = await _load_run_state(run_id)

    if report is None:
        raise HTTPException(
            status_code=404,
            detail=f"No diagnosis report found for run_id={run_id}.",
        )

    # Structured log only -- no effectiveness backfill on 👎 (design §8.1/§8.2:
    # failure attribution is ambiguous; downgrading recalled cases would
    # penalize good cases). retrieved_case_ids is logged for future P1-b
    # failure-pattern mining but not consumed for backfill.
    logger.info(
        "user_downvote",
        run_id=run_id,
        trace_id=trace_id,
        retrieved_case_ids=retrieved_case_ids,
        root_cause=(report.root_cause[:200] if report.root_cause else ""),
        primary_category=report.primary_category,
        confidence=report.confidence,
    )

    return {"ok": True, "run_id": run_id}


# ═════════════════════════════════════════════════════════════════════
# §8.1 path 2: case-level feedback (independent of diagnosis 👍/👎)
# ═════════════════════════════════════════════════════════════════════


class CaseFeedbackRequest(BaseModel):
    """Request body for the case-level feedback endpoint (§8.1 path 2)."""

    case_id: str = Field(..., description="The historical case_id being marked.")
    helpful: bool = Field(
        ...,
        description="True = 有帮助 (backfill effectiveness +delta); "
        "False = 没帮助 (log only, 只升不降).",
    )


@router.post("/{run_id}/case", status_code=200)
async def case_feedback(run_id: str, request: CaseFeedbackRequest) -> dict[str, object]:
    """Mark a referenced historical case helpful / not-helpful (§8.1 path 2).

    Independent of diagnosis 👍/👎 (path 1). Validates ``case_id`` was actually
    referenced by the agent (``case_id ∈ report.referenced_case_ids``) -- only
    cases the agent cited can be marked, preventing arbitrary effectiveness
    inflation. ``helpful=True`` -> ``backfill_effectiveness`` +delta (§8.2
    认可关联分); ``helpful=False`` -> log only (只升不降, §8.2: "没帮助"
    attribution is ambiguous).
    """
    report, _evidence, _trace_id, _retrieved = await _load_run_state(run_id)

    if report is None:
        raise HTTPException(
            status_code=404,
            detail=f"No diagnosis report found for run_id={run_id}.",
        )

    referenced = list(getattr(report, "referenced_case_ids", None) or [])
    if request.case_id not in referenced:
        raise HTTPException(
            status_code=422,
            detail=(
                f"case_id={request.case_id} was not referenced by this diagnosis "
                f"(referenced: {referenced}). Only cases the agent actually "
                "cited can be marked."
            ),
        )

    if request.helpful:
        # Fire-and-forget: don't block the HTTP response on Qdrant I/O. Not
        # idempotent -- each helpful mark adds +delta (consistent with the
        # pre-task-3 👍 backfill; the frontend prevents double-submit).
        async def _backfill() -> None:
            from src.memory.long_term.case_store import backfill_effectiveness

            try:
                updated = await backfill_effectiveness(
                    [request.case_id], delta=EFFECTIVENESS_HELPFUL_DELTA
                )
                logger.info(
                    "case_feedback_backfilled",
                    run_id=run_id,
                    case_id=request.case_id,
                    updated=updated,
                )
            except Exception:
                logger.error(
                    "case_feedback_backfill_failed",
                    run_id=run_id,
                    case_id=request.case_id,
                    exc_info=True,
                )

        asyncio.create_task(_backfill())
    else:
        # "没帮助" -> no backfill (只升不降, §8.2).
        logger.info("case_feedback_not_helpful", run_id=run_id, case_id=request.case_id)

    return {
        "ok": True,
        "run_id": run_id,
        "case_id": request.case_id,
        "helpful": request.helpful,
    }
