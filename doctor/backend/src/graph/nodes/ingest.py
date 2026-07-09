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


def _empty_prefetch() -> dict[str, Any]:
    return {"logs": [], "traces": [], "error_spans": [], "log_count": 0, "trace_count": 0}


async def _prefetch_by_trace_ids(
    trace_ids: list[str],
    start: str,
    end: str,
) -> dict[str, Any]:
    """Precise prefetch by W3C trace_ids — per-case isolation.

    For each trace_id: query Tempo directly (full trace + spans) AND query
    Loki with a `{trace_id=~"tid1|tid2"}` label selector so only this
    trigger's logs/spans are returned. Merges into the same shape as
    :func:`_prefetch_service`.
    """
    from src.tools.observability_unified import search_observability

    all_logs: list[dict[str, Any]] = []
    all_traces: list[dict[str, Any]] = []
    all_error_spans: list[dict[str, Any]] = []

    # Tempo: query each trace_id directly (precise, no time-window pollution).
    for tid in trace_ids:
        try:
            tj = await search_observability(
                source="tempo", query=tid, analysis="full", start=start, end=end
            )
            tdata = json.loads(tj)
            all_traces.extend(tdata.get("traces", []))
            all_error_spans.extend(tdata.get("analysis", {}).get("error_spans", []))
        except Exception as exc:
            logger.warning("prefetch_by_trace_id_tempo_failed", trace_id=tid, error=str(exc))

    # Loki: single query with a trace_id regex selector over the trigger window.
    # Backend service only (api_call triggers hit backend); frontend logs are
    # fetched via the same selector if the frontend OTel labelled them.
    if trace_ids:
        selector = "|".join(trace_ids)
        logql = '{service_name=~"demo-backend|demo-frontend", trace_id=~"' + selector + '"}'
        try:
            lj = await search_observability(
                source="loki", query=logql, start=start, end=end, limit=200
            )
            ldata = json.loads(lj)
            all_logs.extend(ldata.get("logs", []))
        except Exception as exc:
            logger.warning("prefetch_by_trace_id_loki_failed", error=str(exc))

    return {
        "logs": all_logs,
        "traces": all_traces,
        "error_spans": all_error_spans,
        "log_count": len(all_logs),
        "trace_count": len(all_traces),
    }


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
            "trigger_trace_ids": list(getattr(raw, "trigger_trace_ids", []) or []),
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

    # W3C trace_ids for this trigger → precise per-case queries.
    # When present, query by trace_id (Tempo per-trace + Loki label selector)
    # instead of a broad service-wide time window, so each case only sees the
    # signals produced by ITS OWN trigger (no cross-case pollution in batch runs).
    trigger_trace_ids: list[str] = list(getattr(raw, "trigger_trace_ids", []) or [])

    if trigger_trace_ids:
        logger.info(
            "ingest_prefetch_by_trace_ids",
            trace_ids=trigger_trace_ids,
            count=len(trigger_trace_ids),
        )
        trace_results = await _asyncio.gather(
            _prefetch_by_trace_ids(trigger_trace_ids, start, end),
            return_exceptions=True,
        )
        merged = (
            trace_results[0]
            if not isinstance(trace_results[0], BaseException)
            else _empty_prefetch()
        )
        if isinstance(trace_results[0], BaseException):
            logger.warning("ingest_prefetch_by_trace_ids_failed", error=str(trace_results[0]))
        backend = merged
        frontend = _empty_prefetch()
    else:
        # Fallback: broad service-wide window (trigger_time ± 5min)
        svc_results = await _asyncio.gather(
            _prefetch_service(_PREFETCH_SERVICES["backend"], start, end),
            _prefetch_service(_PREFETCH_SERVICES["frontend"], start, end),
            return_exceptions=True,
        )
        backend = (
            svc_results[0] if not isinstance(svc_results[0], BaseException) else _empty_prefetch()
        )
        frontend = (
            svc_results[1] if not isinstance(svc_results[1], BaseException) else _empty_prefetch()
        )
        if isinstance(svc_results[0], BaseException):
            logger.warning("ingest_prefetch_backend_failed", error=str(svc_results[0]))
        if isinstance(svc_results[1], BaseException):
            logger.warning("ingest_prefetch_frontend_failed", error=str(svc_results[1]))

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
        "trigger_trace_ids": trigger_trace_ids,
    }

    normalized = ingest(raw_dict)

    # Attach frontend error spans as metadata for downstream display
    normalized.metadata["frontend_error_spans"] = fe_error_spans

    return {"evidence": normalized}
