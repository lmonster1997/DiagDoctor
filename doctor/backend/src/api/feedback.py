"""
Feedback API — user 👍/👎 on diagnosis reports.

P0 scope:
- ``POST /api/feedback/{run_id}/upvote`` → async index diagnosis into Qdrant
  + §8.1 backfill: credit ``effectiveness``/``hit_count`` on the recalled cases
- ``POST /api/feedback/{run_id}/downvote`` → structured log + §8.1 effectiveness
  downgrade on the recalled cases (P1: failed-case collection)

The ``run_id`` is the LangGraph ``thread_id``, which maps to the checkpoint
state that holds ``report`` (DiagnosisReport) and ``evidence`` (NormalizedEvidence).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException

from src.engine.state import DiagnosisReport, NormalizedEvidence
from src.observability.logger import get_logger

router = APIRouter(prefix="/api/feedback", tags=["feedback"])
logger = get_logger(__name__)

# ── §8.1 effectiveness backfill policy ──────────────────────────────
# The *mechanism* (read-modify-write into Qdrant) lives in
# ``case_store.backfill_effectiveness``; the *policy* -- how much a single
# 👍/👎 moves a recalled case's effectiveness -- lives here, next to the
# trigger. ±0.1 per vote (design §8.1: "如 +0.1, 上限 1.0"); effectiveness is
# clamped to [0, 1] by the mechanism.
EFFECTIVENESS_UPVOTE_DELTA = 0.1
EFFECTIVENESS_DOWNVOTE_DELTA = -0.1


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
    report, evidence, trace_id, retrieved_case_ids = await _load_run_state(run_id)

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
        from src.memory.long_term.case_store import (
            backfill_effectiveness,
            maybe_index_diagnosis,
        )

        try:
            indexed = await maybe_index_diagnosis(
                report=report,
                evidence=evidence,
                source="user_upvote",
                trace_id=trace_id,
                case_id=run_id,  # use thread_id as point id for idempotency
            )
            if indexed:
                logger.info("upvote_indexed", run_id=run_id, trace_id=trace_id)
            else:
                logger.info("upvote_skipped", run_id=run_id, reason="hard_guard")
        except Exception:
            logger.error("upvote_index_failed", run_id=run_id, exc_info=True)

        # §8.1: credit the cases recalled during this diagnosis. Independent
        # of new-case indexing -- a 👍 endorses the diagnosis, which validates
        # the historical references regardless of whether the new case itself
        # landed (e.g. hard-guard skip). backfill_effectiveness degrades
        # internally, so this never throws.
        if retrieved_case_ids:
            updated = await backfill_effectiveness(
                retrieved_case_ids,
                delta=EFFECTIVENESS_UPVOTE_DELTA,
                hit=True,
            )
            logger.info(
                "upvote_backfilled",
                run_id=run_id,
                requested=len(retrieved_case_ids),
                updated=updated,
            )

    asyncio.create_task(_index())

    return {"ok": True, "run_id": run_id}


@router.post("/{run_id}/downvote", status_code=200)
async def downvote(run_id: str) -> dict[str, object]:
    """User 👎: log structured feedback for future failed-case analysis.

    P0: structured logging + §8.1 effectiveness downgrade on the recalled
    cases (a 👎 means the diagnosis was wrong, which reflects on the cases
    that were retrieved to support it).  No failed-case indexing yet.
    P1: failed-case collection — store *why* the diagnosis was wrong
    (agent_root_cause, user_correction) for retrieval-based "曾走过此方向" hints.
    """
    report, evidence, trace_id, retrieved_case_ids = await _load_run_state(run_id)

    if report is None:
        raise HTTPException(
            status_code=404,
            detail=f"No diagnosis report found for run_id={run_id}.",
        )

    logger.info(
        "user_downvote",
        run_id=run_id,
        trace_id=trace_id,
        root_cause=(report.root_cause[:200] if report.root_cause else ""),
        primary_category=report.primary_category,
        confidence=report.confidence,
    )

    # §8.1: downgrade effectiveness on the recalled cases (fire-and-forget,
    # same as upvote's index path). hit=False -- a 👎 is not a confirming hit,
    # so hit_count stays; only effectiveness moves (down, clamped to 0).
    if retrieved_case_ids:
        async def _backfill() -> None:
            from src.memory.long_term.case_store import backfill_effectiveness

            try:
                updated = await backfill_effectiveness(
                    retrieved_case_ids,
                    delta=EFFECTIVENESS_DOWNVOTE_DELTA,
                    hit=False,
                )
                logger.info(
                    "downvote_backfilled",
                    run_id=run_id,
                    requested=len(retrieved_case_ids),
                    updated=updated,
                )
            except Exception:
                logger.error("downvote_backfill_failed", run_id=run_id, exc_info=True)

        asyncio.create_task(_backfill())

    return {"ok": True, "run_id": run_id}
