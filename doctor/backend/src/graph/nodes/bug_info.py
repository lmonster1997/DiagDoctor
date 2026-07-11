"""
BugInfo node — lightweight ingest for CopilotKit chat path.

When a user describes a bug in the CopilotKit chat UI, this node:
    1. Parses the user message to extract bug description, trigger time,
       and W3C trace_ids (via a lightweight LLM call).
    2. Auto-prefetches logs + traces from Loki/Tempo using the extracted
       time window / trace_ids (reuses the same ``_prefetch_*`` functions
       as the full ``ingest_node``).
    3. Runs the deterministic ingest normalization pipeline on the fetched
       data, producing ``NormalizedEvidence`` (golden_signals, correlations,
       timeline) that the downstream ``diagnosis_agent`` node can consume
       identically to the REST API path.

This gives the CopilotKit path the same evidence-gathering power as the
REST path, without requiring structured ``Evidence`` input — just a free-text
user message.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from src.config import settings
from src.graph.state import NormalizedEvidence
from src.ingest.normalizer import ingest
from src.observability.logger import get_logger

logger = get_logger(__name__)

# ── Service LogQL config (mirrors ingest.py) ──────────────────────────

_PREFETCH_SERVICES: dict[str, str] = {
    "backend": '{service_name=~"' + settings.backend_service_name + '"}',
    "frontend": '{service_name=~"' + settings.frontend_service_name + '"}',
}

# ── LLM prompt for extracting structured bug info from free-text ──────

_BUG_INFO_EXTRACTION_PROMPT = """你是一个 Bug 信息提取器。从用户的 Bug 描述中提取以下结构化信息。

- bug_description: 用户描述的 Bug 现象（保留原文）
- trigger_time: ISO 8601 UTC 时间，如 2026-07-11T06:26:51Z。从用户消息中提取。如果用户提到"刚才"/"今天"/"半小时前"等相对时间，请根据参考时间 {current_time} 推算。无法确定则不填
- trace_ids: 用户消息中出现的 W3C trace id 列表（32 位 hex），没有则空数组

用户消息：
{user_message}"""


class BugInfo(BaseModel):
    """Structured bug info extracted from user chat message."""

    bug_description: str = Field(default="", description="用户描述的 Bug 现象（保留原文）")
    trigger_time: str | None = Field(
        default=None,
        description="ISO 8601 UTC 时间，从用户消息中提取，或根据相对时间推算",
    )
    trace_ids: list[str] = Field(
        default_factory=list,
        description="W3C trace id 列表（32 位 hex）",
    )


async def _extract_bug_info(user_message: str) -> dict[str, Any]:
    """Parse user message → {bug_description, trigger_time, trace_ids}.

    Uses triage LLM with ``with_structured_output`` for reliable JSON
    extraction via native tool/function calling — no markdown fence
    cleanup needed.

    Never raises — returns defaults on failure so the downstream
    diagnosis agent can still work with whatever info we have.
    """
    from datetime import datetime, timezone

    from src.llm_factory import get_llm_for_role

    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    prompt = _BUG_INFO_EXTRACTION_PROMPT.format(
        current_time=current_time,
        user_message=user_message,
    )

    try:
        llm = get_llm_for_role("buginfo")
        structured_llm = llm.with_structured_output(BugInfo, method="function_calling")
        bug_info: BugInfo = await structured_llm.ainvoke(prompt)
        logger.debug("bug_info_extracted", bug_info=bug_info.model_dump())
        return bug_info.model_dump()
    except Exception as exc:
        logger.warning("bug_info_extraction_failed", error=str(exc))
        return {
            "bug_description": user_message,
            "trigger_time": None,
            "trace_ids": [],
        }


# ── Prefetch helpers (re-exported from ingest.py for clarity) ────────


async def _prefetch_service(logql: str, start: str, end: str) -> dict[str, Any]:
    """Query Loki+Tempo for one service tier. Never raises."""
    from src.tools.observability_unified import search_observability

    try:
        result_json = await search_observability(
            source="auto", query=logql, start=start, end=end,
            analysis="errors", limit=50,
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
        logger.warning("buginfo_prefetch_service_failed", logql=logql, error=str(exc))
        return {"logs": [], "traces": [], "error_spans": [], "log_count": 0, "trace_count": 0}


async def _prefetch_by_trace_ids(
    trace_ids: list[str], start: str, end: str
) -> dict[str, Any]:
    """Precise prefetch by W3C trace_ids."""
    from src.tools.observability_unified import search_observability

    all_logs: list[dict[str, Any]] = []
    all_traces: list[dict[str, Any]] = []
    all_error_spans: list[dict[str, Any]] = []

    for tid in trace_ids:
        try:
            tj = await search_observability(
                source="tempo", query=tid, analysis="full", start=start, end=end,
            )
            tdata = json.loads(tj)
            all_traces.extend(tdata.get("traces", []))
            all_error_spans.extend(tdata.get("analysis", {}).get("error_spans", []))
        except Exception as exc:
            logger.warning("buginfo_prefetch_tid_tempo_failed", trace_id=tid, error=str(exc))

    if trace_ids:
        selector = "|".join(trace_ids)
        logql = '{service_name=~"demo-backend|demo-frontend", trace_id=~"' + selector + '"}'
        try:
            lj = await search_observability(
                source="loki", query=logql, start=start, end=end, limit=200,
            )
            ldata = json.loads(lj)
            all_logs.extend(ldata.get("logs", []))
        except Exception as exc:
            logger.warning("buginfo_prefetch_tid_loki_failed", error=str(exc))

    return {
        "logs": all_logs,
        "traces": all_traces,
        "error_spans": all_error_spans,
        "log_count": len(all_logs),
        "trace_count": len(all_traces),
    }


def _empty_prefetch() -> dict[str, Any]:
    return {"logs": [], "traces": [], "error_spans": [], "log_count": 0, "trace_count": 0}


def _attach_error_log_excerpts(
    evidence: NormalizedEvidence, logs: list[dict[str, Any]]
) -> None:
    """Attach key error log excerpts to evidence metadata.

    Extracts the first 300 chars of each error-level log line so the
    agent sees exception types (IntegrityError, ForeignKeyViolation,
    etc.) directly in the evidence text without needing an extra
    ``search_observability`` round-trip.

    Only attaches up to 5 excerpts (most recent first) to keep the
    evidence compact.
    """
    error_excerpts: list[str] = []
    for log_entry in logs:
        labels = log_entry.get("labels", {})
        level = str(labels.get("detected_level", labels.get("level", ""))).lower()
        if level not in ("error", "critical"):
            continue
        line = str(log_entry.get("line", log_entry.get("message", "")))
        if not line.strip():
            continue
        # Truncate to first 300 chars — enough to capture exception
        # type and key message without bloating the evidence text.
        excerpt = line[:300]
        if len(line) > 300:
            excerpt += "…"
        error_excerpts.append(excerpt)
        if len(error_excerpts) >= 5:
            break

    if error_excerpts:
        evidence.metadata["error_log_excerpts"] = error_excerpts
        logger.debug(
            "buginfo_error_excerpts_attached",
            count=len(error_excerpts),
        )


# ═════════════════════════════════════════════════════════════════════
# Main node function
# ═════════════════════════════════════════════════════════════════════


async def bug_info_node(state: dict[str, Any]) -> dict[str, Any]:
    """BugInfo node: parse chat message → auto-prefetch → normalize.

    Reads the last user message from ``state["messages"]``, extracts
    structured bug info (description, trigger_time, trace_ids), queries
    Loki/Tempo for relevant logs+traces, and runs the deterministic
    ingest normalization pipeline.

    Args:
        state: CopilotKit state dict with ``messages`` (list of chat messages).

    Returns:
        Dict with ``evidence`` (NormalizedEvidence) and ``bug_info`` metadata
        for downstream nodes.
    """
    # ── Step 1: Extract structured info from user message ─────────
    messages: list = state.get("messages", [])
    user_message = ""
    if messages:
        last_msg = messages[-1]
        if isinstance(last_msg, dict):
            user_message = str(last_msg.get("content", ""))
        elif hasattr(last_msg, "content"):
            user_message = str(last_msg.content)

    if not user_message.strip():
        logger.warning("buginfo_empty_user_message")
        return {"evidence": NormalizedEvidence(), "bug_info": {}}

    bug_info = await _extract_bug_info(user_message)
    trigger_time = bug_info.get("trigger_time")
    trace_ids: list[str] = bug_info.get("trace_ids", []) or []

    logger.info(
        "buginfo_parsed",
        trigger_time=trigger_time,
        trace_count=len(trace_ids),
        desc_preview=bug_info.get("bug_description", "")[:100],
    )

    # ── Narrow search_observability default window ───────────────
    # Match what diagnosis_agent_node does: set trigger_time via
    # ContextVar so the agent's search_observability calls use a
    # ±5min window instead of the noisy "last 1 hour" default.
    if trigger_time:
        try:
            from src.tools.observability_unified import set_trigger_time

            set_trigger_time(trigger_time)
        except ImportError:
            pass

    # ── Step 2: Auto-prefetch from Loki/Tempo ────────────────────
    import asyncio as _asyncio
    from datetime import datetime as dt, timedelta, timezone

    if trigger_time:
        # Normalise timezone: LLM may return "Z", "+00:00", or naive.
        # dt.fromisoformat in 3.11+ handles both; ensure Z→+00:00.
        tt = dt.fromisoformat(trigger_time.replace("Z", "+00:00"))
        # Keep tzinfo — Loki/Tempo require timezone-aware timestamps.
        if tt.tzinfo is None:
            tt = tt.replace(tzinfo=timezone.utc)
        start = (tt - timedelta(minutes=5)).isoformat()
        end = (tt + timedelta(minutes=5)).isoformat()

        if trace_ids:
            logger.info("buginfo_prefetch_by_trace_ids", count=len(trace_ids))
            trace_results = await _asyncio.gather(
                _prefetch_by_trace_ids(trace_ids, start, end),
                return_exceptions=True,
            )
            backend = (
                trace_results[0]
                if not isinstance(trace_results[0], BaseException)
                else _empty_prefetch()
            )
            frontend = _empty_prefetch()
        else:
            svc_results = await _asyncio.gather(
                _prefetch_service(_PREFETCH_SERVICES["backend"], start, end),
                _prefetch_service(_PREFETCH_SERVICES["frontend"], start, end),
                return_exceptions=True,
            )
            backend = (
                svc_results[0]
                if not isinstance(svc_results[0], BaseException)
                else _empty_prefetch()
            )
            frontend = (
                svc_results[1]
                if not isinstance(svc_results[1], BaseException)
                else _empty_prefetch()
            )

        b_logs, b_traces = backend["log_count"], backend["trace_count"]
        f_logs, f_traces = frontend["log_count"], frontend["trace_count"]
        logger.info(
            "buginfo_prefetch_done",
            backend_logs=b_logs, backend_traces=b_traces,
            frontend_logs=f_logs, frontend_traces=f_traces,
        )
    else:
        # No trigger_time → skip prefetch, agent will use tools manually
        logger.info("buginfo_no_trigger_time_skipping_prefetch")
        backend = _empty_prefetch()
        frontend = _empty_prefetch()

    # ── Step 3: Normalize collected data ─────────────────────────
    raw_dict: dict[str, Any] = {
        "user_report": bug_info.get("bug_description", user_message),
        "logs": backend["logs"] + frontend["logs"],
        "traces": backend["traces"] + frontend["traces"],
        "browser_errors": [],  # CopilotKit path has no browser_errors upload
        "trigger_time": trigger_time,
        "trigger_trace_ids": trace_ids,
    }

    normalized = ingest(raw_dict)
    # Attach frontend error spans as metadata
    normalized.metadata["frontend_error_spans"] = frontend.get("error_spans", [])

    # ── Enrich: attach error log excerpts so the agent sees
    #     exception types (IntegrityError, etc.) directly, without
    #     needing an extra search_observability round-trip.
    all_logs = backend["logs"] + frontend["logs"]
    _attach_error_log_excerpts(normalized, all_logs)

    logger.info(
        "buginfo_normalized",
        signal_count=len(normalized.golden_signals),
        correlation_count=len(normalized.correlations),
    )

    return {
        "messages": messages,
        "evidence": normalized,
        "bug_info": bug_info,
    }
