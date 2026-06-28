"""
Observability data fetching tools for DiagDoctor agents.

Provides async functions to query Loki (logs) and Tempo (traces) via their
HTTP APIs, converting raw responses into the domain models (LogEntry, TraceSpan)
defined in `src.graph.state`.

All tools are decorated with @traced for OpenTelemetry span creation and use
aiohttp with configurable timeout and automatic retry.

Usage:
    from src.tools.observability_tools import (
        query_loki_logs,
        query_tempo_trace,
        search_tempo_traces,
    )

    logs = await query_loki_logs(
        logql='{service_name="demo-backend"}',
        start=datetime(...),
        end=datetime(...),
    )
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import aiohttp
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.config import settings
from src.graph.state import LogEntry, TraceSpan
from src.observability.logger import get_logger
from src.observability.tracing import traced

logger = get_logger(__name__)

# ── Constants ───────────────────────────────────────────────────────

DEFAULT_TIMEOUT_SECONDS: float = 30.0
DEFAULT_RETRY_ATTEMPTS: int = 3
DEFAULT_LOG_LIMIT: int = 1000

# Loki API endpoint
LOKI_QUERY_RANGE_PATH: str = "/loki/api/v1/query_range"

# Maximum query time range (Loki enforces ~30d; we clamp to 24h for safety)
MAX_QUERY_RANGE_HOURS: float = 24.0
# Loki's own max query length in hours (as reported in error messages: ~30d)
# We use a slightly tighter clamp to avoid edge cases.
LOKI_MAX_QUERY_LENGTH_HOURS: float = 720.0  # 30 days

# Tempo API endpoints
TEMPO_TRACE_PATH: str = "/api/traces"
TEMPO_SEARCH_PATH: str = "/api/search"


# ── Loki helpers ────────────────────────────────────────────────────


def _handle_loki_response(
    data: dict[str, Any],
) -> list[LogEntry]:
    """Parse Loki query_range response into LogEntry list.

    Loki's query_range response format:
    {
      "status": "success",
      "data": {
        "resultType": "streams",
        "result": [
          {
            "stream": {"service_name": "demo-backend", "level": "error"},
            "values": [
              ["<unix_nano_timestamp>", "<log_line_json>"],
              ...
            ]
          }
        ]
      }
    }
    """
    entries: list[LogEntry] = []

    if data.get("status") != "success":
        logger.warning("loki_query_not_successful", status=data.get("status"))
        return entries

    results = data.get("data", {}).get("result", [])
    for stream in results:
        stream_labels = stream.get("stream", {})
        service = stream_labels.get("service_name", stream_labels.get("service", "unknown"))
        values = stream.get("values", [])
        for value_pair in values:
            try:
                ts_nano = value_pair[0]
                log_line = value_pair[1]

                # Timestamp: Loki returns nanosecond unix epoch as string
                ts_seconds = int(ts_nano) / 1e9
                timestamp = datetime.fromtimestamp(ts_seconds, tz=UTC)

                entry = _parse_log_line(log_line, timestamp, service, stream_labels)
                entries.append(entry)
            except (IndexError, ValueError, KeyError) as exc:
                logger.debug("loki_entry_parse_skip", error=str(exc))
                continue

    return entries


def _parse_log_line(
    log_line: str,
    timestamp: datetime,
    service: str,
    stream_labels: dict[str, str],
) -> LogEntry:
    """Parse a single Loki log line into a LogEntry.

    Attempts to parse the log line as JSON first; falls back to treating
    the entire line as a plain-text message.
    """
    import json as _json

    level = stream_labels.get("level", "info")
    trace_id: str | None = None
    message: str = log_line
    attributes: dict[str, Any] = {}

    try:
        parsed = _json.loads(log_line)
        if isinstance(parsed, dict):
            message = parsed.get("message") or parsed.get("msg") or parsed.get("event") or log_line
            level = parsed.get("level", level)
            trace_id = parsed.get("trace_id") or parsed.get("traceID") or parsed.get("traceId")
            # Store remaining fields as attributes
            attributes = {
                k: v
                for k, v in parsed.items()
                if k
                not in (
                    "message",
                    "msg",
                    "event",
                    "level",
                    "trace_id",
                    "traceID",
                    "traceId",
                    "timestamp",
                    "time",
                    "@timestamp",
                )
            }
    except (ValueError, TypeError):
        pass  # Not JSON, treat entire line as message

    return LogEntry(
        timestamp=timestamp,
        level=level,
        service=service,
        message=message,
        trace_id=trace_id,
        attributes=attributes,
    )


# ── Tempo helpers ───────────────────────────────────────────────────


def _parse_span_name(span_data: dict[str, Any]) -> str:
    """Extract a human-readable span name from OTLP span data."""
    name: object = span_data.get("name", span_data.get("spanName", "unknown"))
    return str(name) if name else "unknown"


def _parse_span_service(span_data: dict[str, Any], resource: dict[str, Any] | None) -> str:
    """Extract service name from OTLP resource attributes."""
    if resource:
        attrs = resource.get("attributes", [])
        for attr in attrs:
            if attr.get("key") == "service.name":
                return str(attr.get("value", {}).get("stringValue", "unknown"))
    # Fallback to process-level or span-level attribute
    attrs = span_data.get("attributes", [])
    for attr in attrs:
        if attr.get("key") == "service.name":
            return str(attr.get("value", {}).get("stringValue", "unknown"))
    return "unknown"


def _parse_span_attributes(span_data: dict[str, Any]) -> dict[str, Any]:
    """Convert OTLP attribute list to a flat dict."""
    result: dict[str, Any] = {}
    attrs = span_data.get("attributes", [])
    for attr in attrs:
        key = attr.get("key", "")
        value_obj = attr.get("value", {})
        value = (
            value_obj.get("stringValue")
            or value_obj.get("intValue")
            or value_obj.get("doubleValue")
            or value_obj.get("boolValue")
        )
        if key:
            result[key] = value
    return result


def _parse_nano_timestamp(nano_str: str) -> datetime:
    """Parse an OTLP nanosecond timestamp string to datetime."""
    try:
        ns = int(nano_str)
        return datetime.fromtimestamp(ns / 1e9, tz=UTC)
    except (ValueError, TypeError):
        logger.debug("tempo_timestamp_parse_fallback", raw=str(nano_str)[:50])
        return datetime.now(tz=UTC)


def _handle_tempo_trace_response(
    data: dict[str, Any],
    trace_id: str,
) -> list[TraceSpan]:
    """Parse Tempo trace response into TraceSpan list.

    Tempo's /api/traces/{trace_id} response follows OTLP format:
    {
      "batches": [
        {
          "resource": { "attributes": [...] },
          "scopeSpans": [
            {
              "scope": { "name": "...", "version": "..." },
              "spans": [
                {
                  "spanId": "...",
                  "parentSpanId": "...",
                  "name": "...",
                  "startTimeUnixNano": "...",
                  "endTimeUnixNano": "...",
                  "status": { "code": 1 },
                  "attributes": [...],
                  ...
                }
              ]
            }
          ]
        }
      ]
    }
    """
    spans: list[TraceSpan] = []

    batches = data.get("batches", [])
    for batch in batches:
        resource = batch.get("resource")
        service = _parse_span_service({}, resource)
        scope_spans = batch.get("scopeSpans", batch.get("instrumentationLibrarySpans", []))
        for scope_span in scope_spans:
            raw_spans = scope_span.get("spans", [])
            for raw_span in raw_spans:
                try:
                    span = _convert_otlp_span_to_trace_span(raw_span, service)
                    spans.append(span)
                except Exception as exc:
                    logger.debug("tempo_span_parse_skip", error=str(exc))
                    continue

    return spans


def _convert_otlp_span_to_trace_span(
    raw_span: dict[str, Any],
    service: str,
) -> TraceSpan:
    """Convert a single OTLP span dict to a TraceSpan model."""
    span_id = raw_span.get("spanId", raw_span.get("spanID", ""))
    parent_id = raw_span.get("parentSpanId", raw_span.get("parentSpanID"))

    start_ns = raw_span.get("startTimeUnixNano", "0")
    end_ns = raw_span.get("endTimeUnixNano", "0")
    start = _parse_nano_timestamp(start_ns)

    try:
        duration_ns = int(end_ns) - int(start_ns)
        duration_ms = duration_ns / 1e6
    except (ValueError, TypeError):
        duration_ms = 0.0

    status_code = raw_span.get("status", {}).get("code", raw_span.get("statusCode", 0))
    if status_code == 0:
        status: Literal["ok", "error", "unset"] = "unset"
    elif status_code == 1:
        status = "ok"
    elif status_code == 2:
        status = "error"
    else:
        status = "unset"

    attributes = _parse_span_attributes(raw_span)

    return TraceSpan(
        span_id=span_id,
        parent_span_id=str(parent_id) if parent_id else "",
        name=_parse_span_name(raw_span),
        service=service,
        start=start,
        duration_ms=duration_ms,
        attributes=attributes,
        status=status,
    )


def _handle_tempo_search_response(
    data: dict[str, Any],
) -> list[dict[str, Any]]:
    """Parse Tempo search response into a list of trace summaries.

    Tempo's /api/search response format varies by version. Common shape:
    {
      "traces": [
        {
          "traceID": "...",
          "rootServiceName": "...",
          "rootTraceName": "...",
          "startTimeUnixNano": "...",
          "durationMs": 1234,
          "spanSets": [...],
        }
      ]
    }
    """
    traces = data.get("traces", [])
    result: list[dict[str, Any]] = []
    for trace in traces:
        result.append(
            {
                "trace_id": trace.get("traceID", trace.get("trace_id", "")),
                "root_service": trace.get("rootServiceName", trace.get("root_service_name", "")),
                "root_name": trace.get("rootTraceName", trace.get("root_trace_name", "")),
                "start_time": _parse_nano_timestamp(
                    trace.get("startTimeUnixNano", "0")
                ).isoformat(),
                "duration_ms": trace.get("durationMs", trace.get("duration_ms", 0)),
                "span_count": len(trace.get("spanSets", [])),
            }
        )
    return result


# ── HTTP client helpers ─────────────────────────────────────────────


def _build_retry_decorator() -> AsyncRetrying:
    """Build a tenacity AsyncRetrying instance with exponential backoff."""
    return AsyncRetrying(
        stop=stop_after_attempt(DEFAULT_RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError, OSError)),
        reraise=True,
    )


async def _http_get_json(
    url: str,
    params: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Perform an HTTP GET request with retry, returning parsed JSON."""
    client_timeout = aiohttp.ClientTimeout(total=timeout)

    async for attempt in _build_retry_decorator():
        with attempt:
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.get(url, params=params) as response:
                    response.raise_for_status()
                    result: dict[str, Any] = await response.json()
                    return result
    # Should not reach here due to reraise=True
    return {}


# ── Public API ──────────────────────────────────────────────────────


@traced("observability.query_loki_logs")
async def query_loki_logs(
    logql: str,
    start: datetime,
    end: datetime,
    limit: int = DEFAULT_LOG_LIMIT,
) -> list[LogEntry]:
    """Query logs from Loki using LogQL.

    Calls the Loki HTTP API `/loki/api/v1/query_range` and converts
    the response into a list of LogEntry domain models.

    The time range is automatically clamped to ``MAX_QUERY_RANGE_HOURS``
    (centered on the midpoint) if the requested span exceeds this limit.
    This prevents Loki 400 errors caused by exceeding its ~30d max query
    length.

    Args:
        logql: The LogQL query string (e.g. '{service_name="demo-backend"}').
        start: Start of the time range.
        end: End of the time range.
        limit: Maximum number of log entries to return (default 1000).

    Returns:
        A list of LogEntry objects parsed from the Loki response.
        Returns an empty list (instead of raising) when Loki rejects
        the query (e.g. time range too wide, no data in range).

    Example:
        >>> from datetime import datetime, timedelta, timezone
        >>> end = datetime.now(timezone.utc)
        >>> start = end - timedelta(hours=1)
        >>> logs = await query_loki_logs(
        ...     logql='{service_name="demo-backend"} |= "error"',
        ...     start=start,
        ...     end=end,
        ... )
    """
    # ── Clamp time range to avoid Loki "time range exceeds limit" (400) ──
    requested_range_hours = (end - start).total_seconds() / 3600.0
    if requested_range_hours > MAX_QUERY_RANGE_HOURS:
        midpoint = start + (end - start) / 2
        half_window = timedelta(hours=MAX_QUERY_RANGE_HOURS / 2)
        original_start, original_end = start, end
        start = midpoint - half_window
        end = midpoint + half_window
        logger.warning(
            "loki_query_time_range_clamped",
            original_start=original_start.isoformat(),
            original_end=original_end.isoformat(),
            original_hours=round(requested_range_hours, 1),
            clamped_start=start.isoformat(),
            clamped_end=end.isoformat(),
            max_hours=MAX_QUERY_RANGE_HOURS,
        )

    base_url = settings.loki_url.rstrip("/")
    url = f"{base_url}{LOKI_QUERY_RANGE_PATH}"

    # Loki expects nanosecond unix timestamps
    start_ns = str(int(start.timestamp() * 1e9))
    end_ns = str(int(end.timestamp() * 1e9))

    params: dict[str, str | int] = {
        "query": logql,
        "start": start_ns,
        "end": end_ns,
        "limit": limit,
        "direction": "forward",
    }

    logger.info(
        "querying_loki_logs",
        logql=logql,
        start=start.isoformat(),
        end=end.isoformat(),
        limit=limit,
    )

    try:
        data = await _http_get_json(url, params=params)
        entries = _handle_loki_response(data)
        logger.info("loki_query_complete", entry_count=len(entries))
        return entries
    except aiohttp.ClientResponseError as exc:
        # Handle Loki-specific 400 errors gracefully (e.g. time range exceeds limit)
        error_text = str(exc)
        if exc.status == 400 and "time range exceeds" in error_text:
            logger.warning(
                "loki_query_time_range_rejected",
                error=error_text,
                logql=logql,
                hint="Loki rejected the query because the time range exceeds its configured limit. "
                "Try a narrower time window (≤24h recommended).",
            )
            return []
        logger.error("loki_query_failed", error=error_text, logql=logql)
        return []
    except Exception as exc:
        logger.error("loki_query_failed", error=str(exc), logql=logql)
        # Return empty list instead of raising to avoid crashing the agent
        return []


@traced("observability.query_tempo_trace")
async def query_tempo_trace(trace_id: str) -> list[TraceSpan]:
    """Query a specific trace from Tempo by trace ID.

    Calls the Tempo HTTP API `/api/traces/{trace_id}` and converts
    the OTLP-format response into a list of TraceSpan domain models.

    Args:
        trace_id: The 32-character hex trace ID (e.g. "a1b2c3d4e5f6...").

    Returns:
        A list of TraceSpan objects representing spans in the trace.

    Example:
        >>> spans = await query_tempo_trace("a1b2c3d4e5f67890abcdef1234567890")
        >>> for s in spans:
        ...     print(s.name, s.duration_ms)
    """
    base_url = settings.tempo_url.rstrip("/")
    url = f"{base_url}{TEMPO_TRACE_PATH}/{trace_id}"

    logger.info("querying_tempo_trace", trace_id=trace_id)

    try:
        data = await _http_get_json(url)
        spans = _handle_tempo_trace_response(data, trace_id)
        logger.info("tempo_trace_query_complete", trace_id=trace_id, span_count=len(spans))
        return spans
    except Exception as exc:
        logger.error("tempo_trace_query_failed", error=str(exc), trace_id=trace_id)
        raise


@traced("observability.search_tempo_traces")
async def search_tempo_traces(
    service: str,
    start: datetime,
    end: datetime,
    min_duration_ms: float | None = None,
) -> list[dict[str, Any]]:
    """Search for traces in Tempo matching the given criteria.

    Calls the Tempo HTTP API `/api/search` with service name, time range,
    and optional minimum duration filter.

    The time range is automatically clamped to ``MAX_QUERY_RANGE_HOURS``
    if the requested span exceeds this limit.

    Args:
        service: The service name to search for (e.g. "demo-backend").
        start: Start of the time range.
        end: End of the time range.
        min_duration_ms: Optional minimum trace duration in milliseconds.

    Returns:
        A list of dicts, each containing summary info for a matching trace:
        - trace_id: str
        - root_service: str
        - root_name: str
        - start_time: ISO format string
        - duration_ms: float
        - span_count: int

    Example:
        >>> end = datetime.now(timezone.utc)
        >>> start = end - timedelta(hours=1)
        >>> traces = await search_tempo_traces(
        ...     service="demo-backend",
        ...     start=start,
        ...     end=end,
        ...     min_duration_ms=500,
        ... )
    """
    # ── Clamp time range ──
    requested_range_hours = (end - start).total_seconds() / 3600.0
    if requested_range_hours > MAX_QUERY_RANGE_HOURS:
        midpoint = start + (end - start) / 2
        half_window = timedelta(hours=MAX_QUERY_RANGE_HOURS / 2)
        start = midpoint - half_window
        end = midpoint + half_window
        logger.warning(
            "tempo_search_time_range_clamped",
            original_hours=round(requested_range_hours, 1),
            clamped_start=start.isoformat(),
            clamped_end=end.isoformat(),
            max_hours=MAX_QUERY_RANGE_HOURS,
        )

    base_url = settings.tempo_url.rstrip("/")
    url = f"{base_url}{TEMPO_SEARCH_PATH}"

    # Tempo search expects epoch seconds or ISO format depending on version
    start_epoch = int(start.timestamp())
    end_epoch = int(end.timestamp())

    params: dict[str, str | int | float] = {
        "start": start_epoch,
        "end": end_epoch,
    }

    # Tempo's search API expects query parameters in a specific format
    # Common format: tags=service.name%3Ddemo-backend
    params["tags"] = f'service.name="{service}"'

    if min_duration_ms is not None:
        params["minDuration"] = f"{min_duration_ms}ms"

    logger.info(
        "searching_tempo_traces",
        service=service,
        start=start.isoformat(),
        end=end.isoformat(),
        min_duration_ms=min_duration_ms,
    )

    try:
        data = await _http_get_json(url, params=params)
        traces = _handle_tempo_search_response(data)
        logger.info("tempo_search_complete", service=service, trace_count=len(traces))
        return traces
    except aiohttp.ClientResponseError as exc:
        logger.warning(
            "tempo_search_http_error",
            error=str(exc),
            status=exc.status,
            service=service,
        )
        return []
    except Exception as exc:
        logger.warning(
            "tempo_search_failed_graceful",
            error=str(exc),
            service=service,
            hint="Returning empty result — the agent will continue without Tempo data.",
        )
        return []
