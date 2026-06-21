"""
Tests for src.tools.observability_tools — Loki & Tempo API clients.

Uses pytest + pytest-asyncio. Mocks aiohttp to avoid real network calls.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from src.graph.state import LogEntry, TraceSpan
from src.tools.observability_tools import (
    _convert_otlp_span_to_trace_span,
    _handle_loki_response,
    _handle_tempo_search_response,
    _handle_tempo_trace_response,
    _parse_log_line,
    _parse_span_attributes,
    _parse_span_name,
    _parse_span_service,
    query_loki_logs,
    query_tempo_trace,
    search_tempo_traces,
)

# ── Test fixtures ───────────────────────────────────────────────────


@pytest.fixture
def sample_timestamp() -> datetime:
    return datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def sample_loki_response() -> dict[str, Any]:
    """A minimal valid Loki query_range response."""
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {
                        "service_name": "demo-backend",
                        "level": "error",
                    },
                    "values": [
                        [
                            "1750000000000000000",  # nanosecond timestamp
                            json.dumps(
                                {
                                    "level": "error",
                                    "message": "Something went wrong",
                                    "trace_id": "abc123def456",
                                    "request_id": "req-001",
                                }
                            ),
                        ],
                        [
                            "1750000001000000000",
                            json.dumps(
                                {
                                    "level": "info",
                                    "message": "Recovered from error",
                                }
                            ),
                        ],
                    ],
                },
            ],
        },
    }


@pytest.fixture
def sample_loki_response_plain_text() -> dict[str, Any]:
    """Loki response with plain-text (non-JSON) log lines."""
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"service_name": "demo-frontend"},
                    "values": [
                        ["1750000000000000000", "[ERROR] Cannot read property 'x' of null"],
                        ["1750000001000000000", "[WARN] Retrying connection..."],
                    ],
                },
            ],
        },
    }


@pytest.fixture
def sample_tempo_trace_response() -> dict[str, Any]:
    """A minimal valid Tempo /api/traces/{id} response in OTLP format."""
    return {
        "batches": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "demo-backend"}},
                    ],
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "fastapi", "version": "1.0"},
                        "spans": [
                            {
                                "spanId": "span-001",
                                "parentSpanId": "",
                                "name": "GET /api/tasks",
                                "startTimeUnixNano": "1750000000000000000",
                                "endTimeUnixNano": "1750000000500000000",
                                "status": {"code": 1},
                                "attributes": [
                                    {"key": "http.method", "value": {"stringValue": "GET"}},
                                    {"key": "http.status_code", "value": {"intValue": "500"}},
                                ],
                            },
                            {
                                "spanId": "span-002",
                                "parentSpanId": "span-001",
                                "name": "SQL SELECT tasks",
                                "startTimeUnixNano": "1750000000100000000",
                                "endTimeUnixNano": "1750000000450000000",
                                "status": {"code": 1},
                                "attributes": [
                                    {
                                        "key": "db.statement",
                                        "value": {"stringValue": "SELECT * FROM tasks"},
                                    },
                                ],
                            },
                        ],
                    },
                ],
            },
        ],
    }


@pytest.fixture
def sample_tempo_search_response() -> dict[str, Any]:
    """A minimal valid Tempo /api/search response."""
    return {
        "traces": [
            {
                "traceID": "abc123def456abc123def456abc12345",
                "rootServiceName": "demo-backend",
                "rootTraceName": "GET /api/tasks",
                "startTimeUnixNano": "1750000000000000000",
                "durationMs": 500.0,
                "spanSets": [{"spans": []}, {"spans": []}],
            },
            {
                "traceID": "fed654cba321fed654cba321fed65432",
                "rootServiceName": "demo-backend",
                "rootTraceName": "POST /api/auth/login",
                "startTimeUnixNano": "1750000100000000000",
                "durationMs": 120.0,
                "spanSets": [],
            },
        ],
    }


# ── Loki response parsing tests ─────────────────────────────────────


class TestLokiResponseParsing:
    """Tests for _handle_loki_response and _parse_log_line."""

    def test_parse_log_line_json(self, sample_timestamp: datetime) -> None:
        """JSON log lines are correctly parsed into LogEntry."""
        log_line = json.dumps(
            {
                "level": "error",
                "message": "Something went wrong",
                "trace_id": "abc123",
            }
        )
        entry = _parse_log_line(log_line, sample_timestamp, "demo-backend", {"level": "error"})

        assert isinstance(entry, LogEntry)
        assert entry.timestamp == sample_timestamp
        assert entry.level == "error"
        assert entry.service == "demo-backend"
        assert entry.message == "Something went wrong"
        assert entry.trace_id == "abc123"

    def test_parse_log_line_plain_text(self, sample_timestamp: datetime) -> None:
        """Plain-text log lines are stored as-is in message field."""
        log_line = "[ERROR] Generic error message"
        entry = _parse_log_line(log_line, sample_timestamp, "demo-frontend", {})

        assert entry.message == log_line
        assert entry.service == "demo-frontend"
        assert entry.level == "info"  # default from empty stream labels

    def test_parse_log_line_json_fallback_message(self, sample_timestamp: datetime) -> None:
        """When JSON has no message/msg/event field, fall back to raw line."""
        log_line = json.dumps({"foo": "bar", "baz": 42})
        entry = _parse_log_line(log_line, sample_timestamp, "svc", {})

        assert entry.message == log_line
        assert entry.attributes == {"foo": "bar", "baz": 42}

    def test_parse_log_line_uses_msg_field(self, sample_timestamp: datetime) -> None:
        """When 'msg' field is present (structlog default), use it as message."""
        log_line = json.dumps({"msg": "Task created", "level": "info"})
        entry = _parse_log_line(log_line, sample_timestamp, "svc", {})

        assert entry.message == "Task created"

    def test_parse_log_line_uses_event_field(self, sample_timestamp: datetime) -> None:
        """When 'event' field is present (structlog), use it as message."""
        log_line = json.dumps({"event": "Request completed", "level": "info"})
        entry = _parse_log_line(log_line, sample_timestamp, "svc", {})

        assert entry.message == "Request completed"

    def test_handle_loki_response_success(self, sample_loki_response: dict[str, Any]) -> None:
        """Full Loki response is correctly parsed into LogEntry list."""
        entries = _handle_loki_response(sample_loki_response)  # type: ignore[arg-type]
        # Note: _handle_loki_response is not async in test context

        assert len(entries) == 2
        assert entries[0].level == "error"
        assert entries[0].service == "demo-backend"
        assert entries[0].trace_id == "abc123def456"
        assert entries[0].message == "Something went wrong"

    def test_handle_loki_response_plain_text(
        self, sample_loki_response_plain_text: dict[str, Any]
    ) -> None:
        """Plain-text Loki response is parsed correctly."""
        entries = _handle_loki_response(sample_loki_response_plain_text)  # type: ignore[arg-type]

        assert len(entries) == 2
        assert entries[0].message == "[ERROR] Cannot read property 'x' of null"
        assert entries[1].message == "[WARN] Retrying connection..."

    def test_handle_loki_response_failure_status(self) -> None:
        """Non-success status returns empty list."""
        entries = _handle_loki_response({"status": "error", "error": "timeout"})  # type: ignore[arg-type]
        assert entries == []

    def test_handle_loki_response_empty(self) -> None:
        """Empty result returns empty list."""
        entries = _handle_loki_response(
            {
                "status": "success",
                "data": {"resultType": "streams", "result": []},
            }
        )  # type: ignore[arg-type]
        assert entries == []


# ── Tempo trace parsing tests ───────────────────────────────────────


class TestTempoTraceParsing:
    """Tests for Tempo trace response parsing."""

    def test_convert_otlp_span(self) -> None:
        """A single OTLP span converts correctly to TraceSpan."""
        raw_span = {
            "spanId": "span-001",
            "parentSpanId": "",
            "name": "GET /api/tasks",
            "startTimeUnixNano": "1750000000000000000",
            "endTimeUnixNano": "1750000000500000000",
            "status": {"code": 1},
            "attributes": [
                {"key": "http.method", "value": {"stringValue": "GET"}},
            ],
        }
        span = _convert_otlp_span_to_trace_span(raw_span, "demo-backend")

        assert isinstance(span, TraceSpan)
        assert span.span_id == "span-001"
        assert span.parent_id is None
        assert span.name == "GET /api/tasks"
        assert span.service == "demo-backend"
        assert span.duration_ms == 500.0
        assert span.status == "ok"
        assert span.attributes == {"http.method": "GET"}

    def test_convert_otlp_span_error_status(self) -> None:
        """Status code 2 maps to 'error'."""
        raw_span = {
            "spanId": "err-span",
            "name": "crash",
            "startTimeUnixNano": "1",
            "endTimeUnixNano": "2",
            "status": {"code": 2},
        }
        span = _convert_otlp_span_to_trace_span(raw_span, "svc")
        assert span.status == "error"

    def test_convert_otlp_span_unset_status(self) -> None:
        """Status code 0 maps to 'unset'."""
        raw_span = {
            "spanId": "u-span",
            "name": "noop",
            "startTimeUnixNano": "1",
            "endTimeUnixNano": "2",
            "status": {"code": 0},
        }
        span = _convert_otlp_span_to_trace_span(raw_span, "svc")
        assert span.status == "unset"

    def test_parse_span_attributes(self) -> None:
        """OTLP attributes list converts to flat dict."""
        raw_span = {
            "attributes": [
                {"key": "http.method", "value": {"stringValue": "GET"}},
                {"key": "http.status_code", "value": {"intValue": "200"}},
                {"key": "http.duration_ms", "value": {"doubleValue": 45.3}},
                {"key": "active", "value": {"boolValue": True}},
            ],
        }
        attrs = _parse_span_attributes(raw_span)
        assert attrs == {
            "http.method": "GET",
            "http.status_code": "200",
            "http.duration_ms": 45.3,
            "active": True,
        }

    def test_parse_span_name(self) -> None:
        """Span name extraction prefers 'name' over 'spanName'."""
        assert _parse_span_name({"name": "GET /api"}) == "GET /api"
        assert _parse_span_name({"spanName": "POST /api"}) == "POST /api"
        assert _parse_span_name({}) == "unknown"

    def test_parse_span_service_from_resource(self) -> None:
        """Service name extracted from resource attributes."""
        resource = {
            "attributes": [
                {"key": "service.name", "value": {"stringValue": "my-service"}},
            ],
        }
        assert _parse_span_service({}, resource) == "my-service"

    def test_parse_span_service_fallback(self) -> None:
        """Service name falls back to span attributes if no resource."""
        span = {
            "attributes": [
                {"key": "service.name", "value": {"stringValue": "from-span"}},
            ],
        }
        assert _parse_span_service(span, None) == "from-span"

    def test_parse_span_service_default(self) -> None:
        """Service name defaults to 'unknown'."""
        assert _parse_span_service({}, None) == "unknown"

    def test_handle_tempo_trace_response(self, sample_tempo_trace_response: dict[str, Any]) -> None:
        """Full Tempo trace response parses correctly."""
        spans = _handle_tempo_trace_response(sample_tempo_trace_response, "trace-id")  # type: ignore[arg-type]

        assert len(spans) == 2
        assert spans[0].span_id == "span-001"
        assert spans[0].name == "GET /api/tasks"
        assert spans[0].service == "demo-backend"
        assert spans[0].status == "ok"
        assert spans[1].span_id == "span-002"
        assert spans[1].parent_id == "span-001"
        assert spans[1].name == "SQL SELECT tasks"
        assert spans[1].duration_ms == 350.0

    def test_handle_tempo_trace_response_empty(self) -> None:
        """Empty batches returns empty list."""
        spans = _handle_tempo_trace_response({"batches": []}, "tid")  # type: ignore[arg-type]
        assert spans == []


# ── Tempo search parsing tests ──────────────────────────────────────


class TestTempoSearchParsing:
    """Tests for Tempo search response parsing."""

    def test_handle_tempo_search_response(
        self, sample_tempo_search_response: dict[str, Any]
    ) -> None:
        """Tempo search response parses correctly."""
        traces = _handle_tempo_search_response(sample_tempo_search_response)  # type: ignore[arg-type]

        assert len(traces) == 2
        assert traces[0]["trace_id"] == "abc123def456abc123def456abc12345"
        assert traces[0]["root_service"] == "demo-backend"
        assert traces[0]["root_name"] == "GET /api/tasks"
        assert traces[0]["duration_ms"] == 500.0
        assert traces[0]["span_count"] == 2

        assert traces[1]["trace_id"] == "fed654cba321fed654cba321fed65432"
        assert traces[1]["duration_ms"] == 120.0

    def test_handle_tempo_search_response_empty(self) -> None:
        """Empty search returns empty list."""
        traces = _handle_tempo_search_response({"traces": []})
        assert traces == []


# ── Integration tests (with mocked aiohttp) ─────────────────────────


class TestQueryLokiLogsIntegration:
    """Integration-style tests for query_loki_logs with mocked HTTP."""

    @pytest.mark.asyncio
    async def test_query_loki_logs_success(self) -> None:
        """query_loki_logs returns parsed LogEntry list on success."""
        mock_response = {
            "status": "success",
            "data": {
                "resultType": "streams",
                "result": [
                    {
                        "stream": {"service_name": "demo-backend", "level": "error"},
                        "values": [
                            [
                                "1750000000000000000",
                                json.dumps({"level": "error", "message": "fail", "trace_id": "t1"}),
                            ],
                        ],
                    },
                ],
            },
        }

        mock_response_obj = MagicMock()
        mock_response_obj.json = AsyncMock(return_value=mock_response)
        mock_response_obj.raise_for_status = MagicMock()
        mock_response_obj.status = 200

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_response_obj)
        mock_cm.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_cm)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            start = datetime(2026, 6, 20, 11, 0, tzinfo=UTC)
            end = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)

            entries = await query_loki_logs(
                logql='{service_name="demo-backend"}',
                start=start,
                end=end,
            )

        assert len(entries) == 1
        assert entries[0].message == "fail"
        assert entries[0].trace_id == "t1"

    @pytest.mark.asyncio
    async def test_query_loki_logs_http_error(self) -> None:
        """query_loki_logs raises on HTTP error after retries."""
        mock_response_obj = MagicMock()
        mock_response_obj.raise_for_status = MagicMock(
            side_effect=__import__("aiohttp").ClientError("Connection refused")
        )

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_response_obj)
        mock_cm.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_cm)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            start = datetime(2026, 6, 20, 11, 0, tzinfo=UTC)
            end = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)

            with pytest.raises(aiohttp.ClientError):
                await query_loki_logs(
                    logql='{service_name="demo-backend"}',
                    start=start,
                    end=end,
                )


class TestQueryTempoTraceIntegration:
    """Integration-style tests for query_tempo_trace with mocked HTTP."""

    @pytest.mark.asyncio
    async def test_query_tempo_trace_success(self) -> None:
        """query_tempo_trace returns parsed TraceSpan list on success."""
        mock_response = {
            "batches": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "demo-backend"}},
                        ],
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "test"},
                            "spans": [
                                {
                                    "spanId": "s1",
                                    "name": "test-span",
                                    "startTimeUnixNano": "1750000000000000000",
                                    "endTimeUnixNano": "1750000000100000000",
                                    "status": {"code": 1},
                                },
                            ],
                        },
                    ],
                },
            ],
        }

        mock_response_obj = MagicMock()
        mock_response_obj.json = AsyncMock(return_value=mock_response)
        mock_response_obj.raise_for_status = MagicMock()

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_response_obj)
        mock_cm.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_cm)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            spans = await query_tempo_trace("abc123")

        assert len(spans) == 1
        assert spans[0].span_id == "s1"
        assert spans[0].name == "test-span"
        assert spans[0].service == "demo-backend"

    @pytest.mark.asyncio
    async def test_query_tempo_trace_http_error(self) -> None:
        """query_tempo_trace raises on HTTP error after retries."""
        mock_response_obj = MagicMock()
        mock_response_obj.raise_for_status = MagicMock(
            side_effect=__import__("aiohttp").ClientError("Not found")
        )

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_response_obj)
        mock_cm.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_cm)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("aiohttp.ClientSession", return_value=mock_session),
            pytest.raises(aiohttp.ClientError),
        ):
            await query_tempo_trace("nonexistent")


class TestSearchTempoTracesIntegration:
    """Integration-style tests for search_tempo_traces with mocked HTTP."""

    @pytest.mark.asyncio
    async def test_search_tempo_traces_success(self) -> None:
        """search_tempo_traces returns parsed trace summaries."""
        mock_response = {
            "traces": [
                {
                    "traceID": "abc123",
                    "rootServiceName": "demo-backend",
                    "rootTraceName": "GET /api",
                    "startTimeUnixNano": "1750000000000000000",
                    "durationMs": 300,
                    "spanSets": [{}],
                },
            ],
        }

        mock_response_obj = MagicMock()
        mock_response_obj.json = AsyncMock(return_value=mock_response)
        mock_response_obj.raise_for_status = MagicMock()

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_response_obj)
        mock_cm.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_cm)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            start = datetime(2026, 6, 20, 11, 0, tzinfo=UTC)
            end = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)

            traces = await search_tempo_traces(
                service="demo-backend",
                start=start,
                end=end,
                min_duration_ms=100,
            )

        assert len(traces) == 1
        assert traces[0]["trace_id"] == "abc123"
        assert traces[0]["duration_ms"] == 300

    @pytest.mark.asyncio
    async def test_search_tempo_traces_no_min_duration(self) -> None:
        """search_tempo_traces works without min_duration_ms."""
        mock_response = {"traces": []}

        mock_response_obj = MagicMock()
        mock_response_obj.json = AsyncMock(return_value=mock_response)
        mock_response_obj.raise_for_status = MagicMock()

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_response_obj)
        mock_cm.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_cm)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            start = datetime(2026, 6, 20, 11, 0, tzinfo=UTC)
            end = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)

            traces = await search_tempo_traces(
                service="demo-backend",
                start=start,
                end=end,
            )

        assert traces == []

    @pytest.mark.asyncio
    async def test_search_tempo_traces_http_error(self) -> None:
        """search_tempo_traces raises on HTTP error after retries."""
        mock_response_obj = MagicMock()
        mock_response_obj.raise_for_status = MagicMock(
            side_effect=__import__("aiohttp").ClientError("Timeout")
        )

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_response_obj)
        mock_cm.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_cm)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            start = datetime(2026, 6, 20, 11, 0, tzinfo=UTC)
            end = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)

            with pytest.raises(aiohttp.ClientError):
                await search_tempo_traces(service="demo-backend", start=start, end=end)


# ── StructuredTool wrapper tests ────────────────────────────────────


class TestStructuredToolWrappers:
    """Verify that LangChain StructuredTool wrappers are correctly configured."""

    def test_loki_query_tool_exists(self) -> None:
        """LOKI_QUERY_TOOL is importable and has correct name/description."""
        from src.tools import LOKI_QUERY_TOOL

        assert LOKI_QUERY_TOOL.name == "query_loki_logs"
        assert "LogQL" in LOKI_QUERY_TOOL.description
        assert LOKI_QUERY_TOOL.coroutine is not None

    def test_tempo_trace_tool_exists(self) -> None:
        """TEMPO_TRACE_TOOL is importable and has correct name/description."""
        from src.tools import TEMPO_TRACE_TOOL

        assert TEMPO_TRACE_TOOL.name == "query_tempo_trace"
        assert "trace id" in TEMPO_TRACE_TOOL.description.lower()
        assert TEMPO_TRACE_TOOL.coroutine is not None

    def test_tempo_search_tool_exists(self) -> None:
        """TEMPO_SEARCH_TOOL is importable and has correct name/description."""
        from src.tools import TEMPO_SEARCH_TOOL

        assert TEMPO_SEARCH_TOOL.name == "search_tempo_traces"
        assert "service name" in TEMPO_SEARCH_TOOL.description.lower()
        assert TEMPO_SEARCH_TOOL.coroutine is not None

    def test_all_tools_exposed_in_all(self) -> None:
        """All three tools are exposed in __all__."""
        from src.tools import __all__ as tools_all

        assert "LOKI_QUERY_TOOL" in tools_all
        assert "TEMPO_TRACE_TOOL" in tools_all
        assert "TEMPO_SEARCH_TOOL" in tools_all
        assert "query_loki_logs" in tools_all
        assert "query_tempo_trace" in tools_all
        assert "search_tempo_traces" in tools_all
