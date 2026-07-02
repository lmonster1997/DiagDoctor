"""
Ingest node — data collection + normalization entry point for the LangGraph.

Two-phase design:
    1. **Collect**: Auto-prefetch logs+traces from Loki/Tempo for both
       backend and frontend services (parallel via asyncio.gather).
    2. **Normalize**: Run the deterministic ingest pipeline
       (denoise→dedup→tree→signals→correlate→index) on the fetched data.

This is a **non-LLM** node — all processing is deterministic rule-based Python.

The fetched data replaces the old file-based Evidence model; user_report and
trigger_time come from the API request, everything else is queried in real-time.
"""

from __future__ import annotations

import json
from typing import Any

from src.config import settings
from src.graph.state import DoctorState
from src.ingest.normalizer import ingest
from src.observability.logger import get_logger

logger = get_logger(__name__)

# ── Service LogQL config (from Settings, env-overridable) ────────────

_PREFETCH_SERVICES: dict[str, str] = {
    "backend": '{service_name=~"' + settings.backend_service_name + '"}',
    "frontend": '{service_name=~"' + settings.frontend_service_name + '"}',
}


async def _prefetch_service(
    logql: str,
    start: str,
    end: str,
) -> dict[str, Any]:
    """Query Loki+Tempo for one service tier (source="auto").

    Returns dict with ``logs``, ``traces``, ``error_spans``, ``log_count``,
    ``trace_count``.  Never raises — returns empty results on failure.
    """
    from src.tools.observability_unified import search_observability

    try:
        result_json = await search_observability(
            source="auto",
            query=logql,
            start=start,
            end=end,
            analysis="errors",
            limit=50,
        )
        data = json.loads(result_json)
        error_spans = data.get("analysis", {}).get("error_spans", [])
        return {
            "logs": data.get("logs", []),
            "traces": data.get("traces", []),
            "error_spans": error_spans,
            "log_count": len(data.get("logs", [])),
            "trace_count": len(data.get("traces", [])),
        }
    except Exception as exc:
        logger.warning("prefetch_service_failed", logql=logql, error=str(exc))
        return {"logs": [], "traces": [], "error_spans": [], "log_count": 0, "trace_count": 0}


# ═════════════════════════════════════════════════════════════════════
# Node function
# ═════════════════════════════════════════════════════════════════════


async def ingest_node(state: DoctorState) -> dict[str, Any]:
    """
    Ingest node: collect observability data → normalize → produce evidence.

    Phase 1 (Collect): Parallel queries to Loki/Tempo for backend + frontend.
    Phase 2 (Normalize): Pass collected logs/traces/browser_errors through
                         the deterministic ingest pipeline.

    Args:
        state: Current DoctorState.  ``raw_evidence.trigger_time`` is the
               only required field; ``raw_evidence.user_report`` is optional.

    Returns:
        Dict with ``evidence`` (NormalizedEvidence) to merge into state.
    """
    raw = state.raw_evidence

    # ── Phase 1: Collect from Loki/Tempo ─────────────────────────
    trigger_time = raw.trigger_time
    if not trigger_time:
        # No trigger_time → nothing to fetch → run ingest on whatever evidence we have
        raw_dict: dict[str, Any] = {
            "user_report": raw.user_report,
            "logs": [log.model_dump() for log in raw.logs],
            "traces": [span.model_dump() for span in raw.traces],
            "browser_errors": [err.model_dump() for err in (raw.browser_errors or [])],
            "trigger_time": raw.trigger_time,
        }
        normalized = ingest(raw_dict)
        return {"evidence": normalized}

    logger.info("ingest_prefetch_start", trigger_time=trigger_time)

    import asyncio as _asyncio
    from datetime import datetime as dt
    from datetime import timedelta

    tt = dt.fromisoformat(trigger_time)
    start = (tt - timedelta(minutes=5)).isoformat()
    end = (tt + timedelta(minutes=5)).isoformat()

    # Parallel fetch backend + frontend
    results = await _asyncio.gather(
        _prefetch_service(_PREFETCH_SERVICES["backend"], start, end),
        _prefetch_service(_PREFETCH_SERVICES["frontend"], start, end),
        return_exceptions=True,
    )

    _empty: dict[str, Any] = {
        "logs": [],
        "traces": [],
        "error_spans": [],
        "log_count": 0,
        "trace_count": 0,
    }
    backend: dict[str, Any] = results[0] if not isinstance(results[0], BaseException) else _empty
    frontend: dict[str, Any] = results[1] if not isinstance(results[1], BaseException) else _empty

    if isinstance(results[0], BaseException):
        logger.warning("ingest_prefetch_backend_failed", error=str(results[0]))
    if isinstance(results[1], BaseException):
        logger.warning("ingest_prefetch_frontend_failed", error=str(results[1]))

    b_logs = backend["log_count"]
    b_traces = backend["trace_count"]
    f_logs = frontend["log_count"]
    f_traces = frontend["trace_count"]
    fe_error_spans = frontend["error_spans"]

    client_error_count = len(
        [s for s in fe_error_spans if s.get("name", "").startswith("client_error")]
    )
    logger.info(
        "ingest_prefetch_done",
        backend_logs=b_logs,
        backend_traces=b_traces,
        frontend_logs=f_logs,
        frontend_traces=f_traces,
        client_error_spans=client_error_count,
    )

    # ── Phase 2: Normalize collected data ────────────────────────
    raw_dict = {
        "user_report": raw.user_report,
        "logs": backend["logs"] + frontend["logs"],
        "traces": backend["traces"] + frontend["traces"],
        "browser_errors": [err.model_dump() for err in (raw.browser_errors or [])],
        "trigger_time": trigger_time,
    }

    normalized = ingest(raw_dict)

    # Attach frontend error spans as metadata for downstream display
    normalized.metadata["frontend_error_spans"] = fe_error_spans

    return {"evidence": normalized}
