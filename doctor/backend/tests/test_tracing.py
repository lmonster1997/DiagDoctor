"""
Tests for src.observability.tracing — OpenTelemetry @traced decorator.
"""

import asyncio

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider

from src.observability.tracing import get_current_span, traced


@pytest.fixture
def isolated_tracer():
    """Set up a clean TracerProvider for testing the traced decorator."""
    provider = TracerProvider()

    # Use the internal _set_tracer_provider to bypass the "only once" guard
    # that prevents tests from overriding an already-set global provider.
    trace_api._set_tracer_provider(provider, log=False)  # type: ignore[attr-defined]  # noqa: SLF001

    yield provider

    # Reset to a fresh provider
    trace_api._set_tracer_provider(TracerProvider(), log=False)  # type: ignore[attr-defined]  # noqa: SLF001


class TestTracedSyncFunction:
    """Tests for @traced on synchronous functions."""

    def test_sync_function_wraps_correctly(self, isolated_tracer: TracerProvider) -> None:
        """The decorated function should still return the correct value."""

        @traced("test-sync-func")
        def add(a: int, b: int) -> int:
            return a + b

        result = add(3, 5)
        assert result == 8

    def test_sync_function_runs_without_error(self, isolated_tracer: TracerProvider) -> None:
        """The decorated sync function should execute without throwing."""

        @traced("test-sync-span")
        def greet(name: str) -> str:
            return f"Hello, {name}"

        result = greet("World")
        assert result == "Hello, World"

    def test_sync_function_default_span_name(self, isolated_tracer: TracerProvider) -> None:
        """Default span name should be derived without error."""

        @traced()
        def my_test_function() -> str:
            return "ok"

        result = my_test_function()
        assert result == "ok"

    def test_sync_function_with_attributes(self, isolated_tracer: TracerProvider) -> None:
        """Span attributes should be passed without error."""

        @traced("attr-test", attributes={"component": "test"})
        def func() -> str:
            return "done"

        result = func()
        assert result == "done"


class TestTracedAsyncFunction:
    """Tests for @traced on asynchronous functions."""

    @pytest.mark.asyncio
    async def test_async_function_returns_correctly(self, isolated_tracer: TracerProvider) -> None:
        """The decorated async function should return the correct value."""

        @traced("test-async-func")
        async def multiply(a: int, b: int) -> int:
            return a * b

        result = await multiply(4, 7)
        assert result == 28

    @pytest.mark.asyncio
    async def test_async_function_runs_without_error(self, isolated_tracer: TracerProvider) -> None:
        """The decorated async function should execute without throwing."""

        @traced("test-async-span")
        async def delayed() -> str:
            await asyncio.sleep(0.01)
            return "done"

        result = await delayed()
        assert result == "done"

    @pytest.mark.asyncio
    async def test_async_function_default_name(self, isolated_tracer: TracerProvider) -> None:
        """Default span name should be derived without error."""

        @traced()
        async def async_default_name() -> bool:
            return True

        result = await async_default_name()
        assert result is True


class TestGetCurrentSpan:
    """Tests for get_current_span."""

    def test_get_current_span_returns_span(self) -> None:
        """Should return a span (even if INVALID)."""
        span = get_current_span()
        # Should not be None; OTel always returns a span (possibly INVALID)
        assert span is not None

    def test_get_current_span_inside_traced(self, isolated_tracer: TracerProvider) -> None:
        """Inside a @traced function, the current span should be valid."""

        @traced("capture-span")
        def capture() -> bool:
            span = get_current_span()
            return span.is_recording()

        result = capture()
        # With a TracerProvider in place, is_recording should be True
        assert result is True
