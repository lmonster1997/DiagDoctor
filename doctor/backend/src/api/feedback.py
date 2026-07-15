"""
Feedback API — user 👍/👎 on diagnosis reports.

P0 scope:
- ``POST /api/feedback/{run_id}/upvote`` → async index diagnosis into Qdrant
- ``POST /api/feedback/{run_id}/downvote`` → structured log only (P1: failed-case collection)

The ``run_id`` is the LangGraph ``thread_id``, which maps to the checkpoint
state that holds ``report`` (DiagnosisReport) and ``evidence`` (NormalizedEvidence).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from src.engine.state import DiagnosisReport, NormalizedEvidence
from src.observability.logger import get_logger

router = APIRouter(prefix="/api/feedback", tags=["feedback"])
logger = get_logger(__name__)


# ── Internal helpers ────────────────────────────────────────────────


async def _load_run_state(
    run_id: str,
) -> tuple[DiagnosisReport | None, NormalizedEvidence | None, str]:
    """Load DiagnosisReport + NormalizedEvidence from LangGraph checkpoint.

    The MemorySaver checkpointer stores graph state keyed by ``thread_id``
    (which equals ``run_id``).  We fetch the final state snapshot.

    Returns:
        (report, evidence, trace_id) — any may be None/empty if not found.
    """
    from src.engine.nodes.diagnosis_agent import get_copilotkit_graph

    graph = get_copilotkit_graph()
    config = {"configurable": {"thread_id": run_id}}

    try:
        snapshot = await graph.aget_state(config)
    except Exception:
        logger.warning("feedback_load_state_failed", run_id=run_id, exc_info=True)
        return None, None, ""

    if snapshot is None or snapshot.values is None:
        return None, None, ""

    state: dict = snapshot.values
    report = state.get("report")
    evidence = state.get("evidence")

    # Resolve trace_id: first from trigger_trace_ids, then metadata
    trace_id = ""
    if evidence is not None and isinstance(evidence, NormalizedEvidence):
        if evidence.trigger_trace_ids:
            trace_id = evidence.trigger_trace_ids[0]
        elif evidence.metadata:
            trace_id = evidence.metadata.get("trace_id", "")

    return report, evidence, trace_id


# ═════════════════════════════════════════════════════════════════════
# Routes
# ═════════════════════════════════════════════════════════════════════


@router.post("/{run_id}/upvote", status_code=200)
async def upvote(run_id: str) -> dict[str, object]:
    """User 👍: index the diagnosis report into historical_cases (Qdrant).

    This is the **only** P0 indexing trigger.  The write is async
    (``asyncio.create_task``) — the HTTP response returns immediately.
    """
    report, evidence, trace_id = await _load_run_state(run_id)

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
    async def _index():
        from src.memory.long_term.case_store import maybe_index_diagnosis

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

    asyncio.create_task(_index())

    return {"ok": True, "run_id": run_id}


@router.post("/{run_id}/downvote", status_code=200)
async def downvote(run_id: str) -> dict[str, object]:
    """User 👎: log structured feedback for future failed-case analysis.

    P0: no scoring, no indexing.  Only structured logging.
    P1: failed-case collection — store *why* the diagnosis was wrong
    (agent_root_cause, user_correction) for retrieval-based "曾走过此方向" hints.
    """
    report, evidence, trace_id = await _load_run_state(run_id)

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

    return {"ok": True, "run_id": run_id}
