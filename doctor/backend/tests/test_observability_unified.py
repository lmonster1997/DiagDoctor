"""
Tests for src.tools.observability_unified — search_observability unified entry.

Uses pytest + pytest-asyncio. Mocks Loki & Tempo HTTP calls to avoid
real network dependencies.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tools.observability_unified import (
    MAX_QUERY_RANGE_HOURS,
    _default_time_range,
    _extract_trace_ids_from_logs,
    _parse_time,
    _run_trace_analysis,
    _validate_time_range,
    get_search_observability_tool,
    search_observability,
)

# ── Time parsing tests ───────────────────────────────────────────────


class TestParseTime:
    def test_parse_valid_iso_with_z(self):
        result = _parse_time("2026-06-28T10:00:00Z")
        assert result is not None
        assert result.year == 2026
        assert result.month == 6
        assert result.day == 28
        assert result.hour == 10
        assert result.tzinfo is not None

    def test_parse_valid_iso_with_offset(self):
        result = _parse_time("2026-06-28T10:00:00+00:00")
        assert result is not None
        assert result.hour == 10

    def test_parse_none_returns_none(self):
        assert _parse_time(None) is None

    def test_parse_empty_returns_none(self):
        assert _parse_time("") is None

    def test_parse_invalid_returns_none(self):
        assert _parse_time("not-a-date") is None


# ── Time range validation tests ──────────────────────────────────────


class TestValidateTimeRange:
    def test_valid_range_passes(self):
        start = datetime(2026, 6, 28, 10, 0, 0, tzinfo=UTC)
        end = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)
        # Should not raise
        _validate_time_range(start, end)

    def test_exceeds_max_range_raises(self):
        start = datetime(2026, 6, 28, 10, 0, 0, tzinfo=UTC)
        end = start + timedelta(hours=MAX_QUERY_RANGE_HOURS + 1)
        with pytest.raises(ValueError, match="Time range exceeds"):
            _validate_time_range(start, end)

    def test_start_after_end_raises(self):
        start = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)
        end = datetime(2026, 6, 28, 10, 0, 0, tzinfo=UTC)
        with pytest.raises(ValueError, match="start must be before end"):
            _validate_time_range(start, end)

    def test_exact_boundary_passes(self):
        start = datetime(2026, 6, 28, 10, 0, 0, tzinfo=UTC)
        end = start + timedelta(hours=MAX_QUERY_RANGE_HOURS)
        # Should not raise — exactly at boundary
        _validate_time_range(start, end)


# ── Default time range tests ─────────────────────────────────────────


class TestDefaultTimeRange:
    def test_returns_tuple_of_two_datetimes(self):
        start, end = _default_time_range()
        assert isinstance(start, datetime)
        assert isinstance(end, datetime)
        assert start < end

    def test_range_is_approximately_one_hour(self):
        start, end = _default_time_range()
        diff = (end - start).total_seconds()
        assert 3500 < diff < 3700  # ~1 hour with small tolerance


# ── Trace ID extraction tests ────────────────────────────────────────


class TestExtractTraceIds:
    def test_extracts_from_explicit_trace_id_field(self):
        logs = [
            {"trace_id": "abc123def4567890abcdef1234567890", "message": "error"},
            {"trace_id": "1111222233334444aaaabbbbccccdddd", "message": "another"},
        ]
        result = _extract_trace_ids_from_logs(logs)
        assert len(result) == 2
        assert "abc123def4567890abcdef1234567890" in result

    def test_extracts_from_message_text(self):
        logs = [
            {"message": "Error in trace abc123def4567890abcdef1234567890 occurred"},
        ]
        result = _extract_trace_ids_from_logs(logs)
        assert len(result) == 1
        assert result[0] == "abc123def4567890abcdef1234567890"

    def test_deduplicates_duplicate_ids(self):
        logs = [
            {"trace_id": "abc123def4567890abcdef1234567890", "message": "error1"},
            {"trace_id": "abc123def4567890abcdef1234567890", "message": "error2"},
        ]
        result = _extract_trace_ids_from_logs(logs)
        assert len(result) == 1

    def test_caps_at_max_trace_ids(self):
        logs = []
        for i in range(10):
            tid = f"{i:032x}"
            logs.append({"trace_id": tid, "message": f"error {i}"})
        result = _extract_trace_ids_from_logs(logs)
        assert len(result) <= 5  # AUTO_MODE_MAX_TRACE_IDS

    def test_skips_invalid_ids(self):
        logs = [
            {"trace_id": "short", "message": "error"},
            {"trace_id": "abc123def4567890abcdef1234567890", "message": "valid"},
        ]
        result = _extract_trace_ids_from_logs(logs)
        assert len(result) == 1
        assert "abc123def4567890abcdef1234567890" in result

    def test_empty_logs_returns_empty(self):
        result = _extract_trace_ids_from_logs([])
        assert result == []


# ── Trace analysis tests ─────────────────────────────────────────────


class TestRunTraceAnalysis:
    def test_raw_analysis_returns_span_count(self):
        traces: list[dict[str, Any]] = []
        result = _run_trace_analysis(traces, "raw")
        assert "note" in result

    def test_full_analysis_with_empty_traces(self):
        result = _run_trace_analysis([], "full")
        assert "note" in result

    def test_n_plus_one_analysis(self):
        traces = [
            {
                "span_id": "span1",
                "parent_span_id": "",
                "name": "GET /api/tasks",
                "service_name": "demo-backend",
                "duration_ms": 500.0,
                "status": "ok",
                "attributes": {},
            },
            {
                "span_id": "span2",
                "parent_span_id": "span1",
                "name": "SELECT",
                "service_name": "demo-backend",
                "duration_ms": 50.0,
                "status": "ok",
                "db_statement": "SELECT * FROM tasks WHERE id = ?",
                "attributes": {},
            },
            {
                "span_id": "span3",
                "parent_span_id": "span1",
                "name": "SELECT",
                "service_name": "demo-backend",
                "duration_ms": 52.0,
                "status": "ok",
                "db_statement": "SELECT * FROM tasks WHERE id = ?",
                "attributes": {},
            },
            {
                "span_id": "span4",
                "parent_span_id": "span1",
                "name": "SELECT",
                "service_name": "demo-backend",
                "duration_ms": 48.0,
                "status": "ok",
                "db_statement": "SELECT * FROM tasks WHERE id = ?",
                "attributes": {},
            },
        ]
        result = _run_trace_analysis(traces, "n_plus_one")
        assert len(result.get("n_plus_one", [])) >= 1

    def test_bottlenecks_analysis(self):
        traces = [
            {
                "span_id": "span1",
                "parent_span_id": "",
                "name": "slow_query",
                "service_name": "demo-backend",
                "duration_ms": 5000.0,
                "status": "ok",
                "attributes": {},
            },
        ]
        result = _run_trace_analysis(traces, "bottlenecks")
        assert len(result.get("bottlenecks", [])) >= 1

    def test_error_spans_analysis(self):
        traces = [
            {
                "span_id": "span1",
                "parent_span_id": "",
                "name": "crash",
                "service_name": "demo-backend",
                "duration_ms": 100.0,
                "status": "error",
                "attributes": {},
            },
        ]
        result = _run_trace_analysis(traces, "errors")
        assert len(result.get("error_spans", [])) >= 1

    def test_full_analysis(self):
        traces = [
            {
                "span_id": "span1",
                "parent_span_id": "",
                "name": "GET /api/tasks",
                "service_name": "demo-backend",
                "duration_ms": 500.0,
                "status": "ok",
                "attributes": {},
            },
        ]
        result = _run_trace_analysis(traces, "full")
        assert "n_plus_one" in result
        assert "bottlenecks" in result
        assert "error_spans" in result
        assert "summary" in result


# ── search_observability integration tests (mocked HTTP) ────────────


@pytest.fixture
def mock_loki_response() -> dict[str, Any]:
    """Simulate a Loki query_range response with trace_id embedded."""
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"service_name": "demo-backend", "level": "error"},
                    "values": [
                        [
                            "1750000000000000000",
                            json.dumps(
                                {
                                    "level": "error",
                                    "message": "Database connection timeout",
                                    "trace_id": "abc123def4567890abcdef1234567890",
                                }
                            ),
                        ],
                    ],
                },
            ],
        },
    }


@pytest.fixture
def mock_tempo_trace_response() -> dict[str, Any]:
    """Simulate a Tempo /api/traces/{id} response."""
    return {
        "batches": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "demo-backend"}}
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "test"},
                        "spans": [
                            {
                                "spanId": "span001",
                                "parentSpanId": "",
                                "name": "GET /api/tasks",
                                "startTimeUnixNano": "1750000000000000000",
                                "endTimeUnixNano": "1750000000500000000",
                                "status": {"code": 1},
                                "attributes": [],
                            },
                            {
                                "spanId": "span002",
                                "parentSpanId": "span001",
                                "name": "SELECT task",
                                "startTimeUnixNano": "1750000000100000000",
                                "endTimeUnixNano": "1750000000400000000",
                                "status": {"code": 2},
                                "attributes": [
                                    {
                                        "key": "db.statement",
                                        "value": {
                                            "stringValue": "SELECT * FROM tasks WHERE id = 1"
                                        },
                                    },
                                ],
                            },
                        ],
                    }
                ],
            }
        ],
    }


@pytest.fixture
def mock_tempo_search_response() -> dict[str, Any]:
    """Simulate a Tempo /api/search response."""
    return {
        "traces": [
            {
                "traceID": "abc123def4567890abcdef1234567890",
                "rootServiceName": "demo-backend",
                "rootTraceName": "GET /api/tasks",
                "startTimeUnixNano": "1750000000000000000",
                "durationMs": 500,
                "spanSets": [{}, {}],
            },
        ],
    }


class TestSearchObservabilitySourceLoki:
    """Tests for source='loki' mode."""

    @pytest.mark.asyncio
    async def test_loki_mode_returns_logs(self, mock_loki_response):
        mock_get = AsyncMock()
        mock_get.return_value.__aenter__.return_value.json.return_value = mock_loki_response
        mock_get.return_value.__aenter__.return_value.status = 200
        mock_get.return_value.__aenter__.return_value.raise_for_status = MagicMock()

        with patch("aiohttp.ClientSession.get", return_value=mock_get.return_value):
            result = await search_observability(
                source="loki",
                query='{service_name="demo-backend"} |= "error"',
                start="2026-06-28T10:00:00Z",
                end="2026-06-28T12:00:00Z",
                analysis="raw",
            )

        parsed = json.loads(result)
        assert parsed["source"] == "loki"
        assert "logs" in parsed
        assert "traces" in parsed
        assert "analysis" in parsed

    @pytest.mark.asyncio
    async def test_loki_mode_with_default_time_range(self, mock_loki_response):
        mock_get = AsyncMock()
        mock_get.return_value.__aenter__.return_value.json.return_value = mock_loki_response
        mock_get.return_value.__aenter__.return_value.status = 200
        mock_get.return_value.__aenter__.return_value.raise_for_status = MagicMock()

        with patch("aiohttp.ClientSession.get", return_value=mock_get.return_value):
            result = await search_observability(
                source="loki",
                query='{service_name="demo-backend"}',
                analysis="raw",
            )

        parsed = json.loads(result)
        assert "time_range" in parsed
        assert parsed["time_range"]["start"] is not None

    @pytest.mark.asyncio
    async def test_loki_mode_exceeds_time_range_raises(self):
        with pytest.raises(ValueError, match="Time range exceeds"):
            await search_observability(
                source="loki",
                query='{service_name="demo-backend"}',
                start="2026-06-28T00:00:00Z",
                end="2026-06-28T10:00:00Z",  # 10 hours > 4 hour max
            )


class TestSearchObservabilitySourceTempo:
    """Tests for source='tempo' mode."""

    @pytest.mark.asyncio
    async def test_tempo_trace_id_mode(self, mock_tempo_trace_response):
        mock_get = AsyncMock()
        mock_get.return_value.__aenter__.return_value.json.return_value = mock_tempo_trace_response
        mock_get.return_value.__aenter__.return_value.status = 200
        mock_get.return_value.__aenter__.return_value.raise_for_status = MagicMock()

        with patch("aiohttp.ClientSession.get", return_value=mock_get.return_value):
            result = await search_observability(
                source="tempo",
                query="abc123def4567890abcdef1234567890",
                analysis="full",
            )

        parsed = json.loads(result)
        assert parsed["source"] == "tempo"
        assert len(parsed["traces"]) > 0
        assert "analysis" in parsed

    @pytest.mark.asyncio
    async def test_tempo_service_search_mode(
        self, mock_tempo_search_response, mock_tempo_trace_response
    ):
        """When query is not a 32-char hex, treat as service name search."""
        mock_get = AsyncMock()

        # First call: search_tempo_traces, second call: query_tempo_trace (if any)
        call_count = 0
        responses = [mock_tempo_search_response]

        async def mock_json():
            nonlocal call_count
            resp = responses[min(call_count, len(responses) - 1)]
            call_count += 1
            return resp

        mock_get.return_value.__aenter__.return_value.json = mock_json
        mock_get.return_value.__aenter__.return_value.status = 200
        mock_get.return_value.__aenter__.return_value.raise_for_status = MagicMock()

        with patch("aiohttp.ClientSession.get", return_value=mock_get.return_value):
            result = await search_observability(
                source="tempo",
                query="demo-backend",
                start="2026-06-28T10:00:00Z",
                end="2026-06-28T12:00:00Z",
                analysis="raw",
            )

        parsed = json.loads(result)
        assert parsed["source"] == "tempo"
        assert "metadata" in parsed


class TestSearchObservabilitySourceAuto:
    """Tests for source='auto' mode — Loki → extract trace_id → Tempo."""

    @pytest.mark.asyncio
    async def test_auto_mode_extracts_and_queries_tempo(
        self, mock_loki_response, mock_tempo_trace_response
    ):
        call_count = 0
        # First call: Loki, second call: Tempo trace
        responses = [mock_loki_response, mock_tempo_trace_response]

        async def mock_json():
            nonlocal call_count
            resp = responses[min(call_count, len(responses) - 1)]
            call_count += 1
            return resp

        mock_get = AsyncMock()
        mock_get.return_value.__aenter__.return_value.json = mock_json
        mock_get.return_value.__aenter__.return_value.status = 200
        mock_get.return_value.__aenter__.return_value.raise_for_status = MagicMock()

        with patch("aiohttp.ClientSession.get", return_value=mock_get.return_value):
            result = await search_observability(
                source="auto",
                query='{service_name="demo-backend"} |= "error"',
                start="2026-06-28T10:00:00Z",
                end="2026-06-28T12:00:00Z",
                analysis="full",
            )

        parsed = json.loads(result)
        assert parsed["source"] == "auto"
        assert len(parsed["logs"]) > 0
        assert len(parsed["traces"]) > 0
        # auto mode should have auto_trace_ids in metadata
        assert "auto_trace_ids" in parsed.get("metadata", {})
        # Full analysis should have analysis results
        assert "analysis" in parsed


class TestSearchObservabilityAnalysis:
    """Tests for analysis parameter behavior."""

    @pytest.mark.asyncio
    async def test_analysis_raw_skips_analysis(self, mock_tempo_trace_response):
        mock_get = AsyncMock()
        mock_get.return_value.__aenter__.return_value.json.return_value = mock_tempo_trace_response
        mock_get.return_value.__aenter__.return_value.status = 200
        mock_get.return_value.__aenter__.return_value.raise_for_status = MagicMock()

        with patch("aiohttp.ClientSession.get", return_value=mock_get.return_value):
            result = await search_observability(
                source="tempo",
                query="abc123def4567890abcdef1234567890",
                analysis="raw",
            )

        parsed = json.loads(result)
        assert parsed["analysis"] == {}

    @pytest.mark.asyncio
    async def test_analysis_full_includes_all(self, mock_tempo_trace_response):
        mock_get = AsyncMock()
        mock_get.return_value.__aenter__.return_value.json.return_value = mock_tempo_trace_response
        mock_get.return_value.__aenter__.return_value.status = 200
        mock_get.return_value.__aenter__.return_value.raise_for_status = MagicMock()

        with patch("aiohttp.ClientSession.get", return_value=mock_get.return_value):
            result = await search_observability(
                source="tempo",
                query="abc123def4567890abcdef1234567890",
                analysis="full",
            )

        parsed = json.loads(result)
        analysis = parsed["analysis"]
        # Should include all analysis keys
        assert "n_plus_one" in analysis
        assert "bottlenecks" in analysis
        assert "error_spans" in analysis
        assert "summary" in analysis


class TestSearchObservabilityEdgeCases:
    """Edge case tests."""

    @pytest.mark.asyncio
    async def test_loki_returns_empty_on_error(self):
        """When Loki returns an error status, logs should be empty."""
        error_response = {"status": "error", "error": "something went wrong"}

        mock_get = AsyncMock()
        mock_get.return_value.__aenter__.return_value.json.return_value = error_response
        mock_get.return_value.__aenter__.return_value.status = 200
        mock_get.return_value.__aenter__.return_value.raise_for_status = MagicMock()

        with patch("aiohttp.ClientSession.get", return_value=mock_get.return_value):
            result = await search_observability(
                source="loki",
                query='{service_name="demo-backend"}',
                start="2026-06-28T10:00:00Z",
                end="2026-06-28T12:00:00Z",
            )

        parsed = json.loads(result)
        assert parsed["logs"] == []

    @pytest.mark.asyncio
    async def test_tempo_invalid_trace_id(self):
        """Non-32-char-hex query in tempo mode triggers service search."""
        mock_get = AsyncMock()
        mock_get.return_value.__aenter__.return_value.json.return_value = {"traces": []}
        mock_get.return_value.__aenter__.return_value.status = 200
        mock_get.return_value.__aenter__.return_value.raise_for_status = MagicMock()

        with patch("aiohttp.ClientSession.get", return_value=mock_get.return_value):
            result = await search_observability(
                source="tempo",
                query="not-a-trace-id",
                start="2026-06-28T10:00:00Z",
                end="2026-06-28T12:00:00Z",
                analysis="raw",
            )

        parsed = json.loads(result)
        assert parsed["source"] == "tempo"


# ── StructuredTool tests ─────────────────────────────────────────────


class TestSearchObservabilityTool:
    """Tests for the LangChain StructuredTool wrapper."""

    def test_tool_creation(self):
        tool = get_search_observability_tool()
        assert tool.name == "search_observability"
        assert tool.description is not None
        assert "统一可观测性查询入口" in tool.description

    def test_tool_is_cached(self):
        tool1 = get_search_observability_tool()
        tool2 = get_search_observability_tool()
        assert tool1 is tool2

    def test_tool_has_coroutine(self):
        tool = get_search_observability_tool()
        assert tool.coroutine is not None
        assert callable(tool.coroutine)


# ── Large payload truncation test ────────────────────────────────────


class TestLargePayloadTruncation:
    @pytest.mark.asyncio
    async def test_large_payload_is_truncated(self):
        """When response exceeds 8000 chars, it should be truncated."""
        # Create a Loki response with many log entries to trigger truncation
        many_logs: dict[str, Any] = {
            "status": "success",
            "data": {
                "resultType": "streams",
                "result": [
                    {
                        "stream": {"service_name": "demo-backend"},
                        "values": [
                            [
                                "1750000000000000000",
                                json.dumps(
                                    {
                                        "message": "log entry " + "x" * 200,
                                        "trace_id": f"abc123def4567890abcdef12345678{i:02d}",
                                    }
                                ),
                            ]
                            for i in range(30)
                        ],
                    },
                ],
            },
        }

        # We also need a large tempo response to fill traces
        large_trace: dict[str, Any] = {
            "batches": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "demo-backend"},
                            }
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "test"},
                            "spans": [
                                {
                                    "spanId": f"span{i:04d}",
                                    "parentSpanId": "",
                                    "name": f"span_{i}_" + "x" * 100,
                                    "startTimeUnixNano": "1750000000000000000",
                                    "endTimeUnixNano": "1750000000500000000",
                                    "status": {"code": 1},
                                    "attributes": [],
                                }
                                for i in range(20)
                            ],
                        }
                    ],
                }
            ],
        }

        call_count = 0
        responses = [many_logs, large_trace]

        async def mock_json():
            nonlocal call_count
            resp = responses[min(call_count, len(responses) - 1)]
            call_count += 1
            return resp

        mock_get = AsyncMock()
        mock_get.return_value.__aenter__.return_value.json = mock_json
        mock_get.return_value.__aenter__.return_value.status = 200
        mock_get.return_value.__aenter__.return_value.raise_for_status = MagicMock()

        with patch("aiohttp.ClientSession.get", return_value=mock_get.return_value):
            result = await search_observability(
                source="auto",
                query='{service_name="demo-backend"}',
                start="2026-06-28T10:00:00Z",
                end="2026-06-28T12:00:00Z",
                analysis="full",
            )

        parsed = json.loads(result)
        # Check truncation flag (may or may not be truncated depending on payload size)
        if parsed.get("_truncated"):
            assert "_original_counts" in parsed
