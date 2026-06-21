"""
Tracing utilities for the Doctor agent.

Provides a @traced decorator for automatic OpenTelemetry span creation and
integration with the observability stack.

Usage:
    from src.observability.tracing import traced

    @traced("my-operation")
    async def do_something(arg: str) -> str:
        ...

    # Span name defaults to the function's qualified name
    @traced()
    def sync_func() -> None:
        ...
"""

import inspect
import logging
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

from opentelemetry import trace

logger = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable[..., Any])


def _get_tracer() -> trace.Tracer:
    """Get an OpenTelemetry tracer for this module."""
    return trace.get_tracer(__name__)


def traced(
    name: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> Callable[[_F], _F]:
    """
    Decorator to automatically create an OpenTelemetry span around a function.

    Works with both synchronous and asynchronous functions. If OpenTelemetry
    is not configured (no TracerProvider set), the function is called normally
    without any overhead beyond a no-op span.

    Args:
        name: Optional span name (defaults to the function's qualified name).
        attributes: Optional span attributes to set on every invocation.

    Returns:
        A decorated function that wraps the original in an OTel span.

    Example:
        >>> @traced("diagnose-triage")
        ... async def triage_node(state: DoctorState) -> dict:
        ...     ...
    """

    def decorator(func: _F) -> _F:
        span_name = name or func.__qualname__
        span_attrs = attributes or {}

        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = _get_tracer()
            with tracer.start_as_current_span(span_name, attributes=span_attrs):
                return await func(*args, **kwargs)

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = _get_tracer()
            with tracer.start_as_current_span(span_name, attributes=span_attrs):
                return func(*args, **kwargs)

        if inspect.iscoroutinefunction(func):
            return async_wrapper  # type: ignore[return-value]
        return sync_wrapper  # type: ignore[return-value]

    return decorator


def get_current_span() -> trace.Span:
    """Get the currently active OpenTelemetry span.

    Returns:
        The active span, or an INVALID span if no span is active.
    """
    return trace.get_current_span()
