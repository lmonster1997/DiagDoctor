"""
Tracing utilities for the Doctor agent.

Provides a @traced decorator for automatic span creation and
integration with the observability stack.
"""

from collections.abc import Callable
from functools import wraps

from opentelemetry import trace


def traced(name: str | None = None, attributes: dict | None = None) -> Callable:
    """
    Decorator to automatically create an OpenTelemetry span around a function.

    Args:
        name: Optional span name (defaults to function name).
        attributes: Optional span attributes.

    Returns:
        Decorated function.
    """

    def decorator(func: Callable) -> Callable:
        span_name = name or func.__qualname__

        @wraps(func)
        async def async_wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
            tracer = trace.get_tracer(__name__)
            with tracer.start_as_current_span(span_name, attributes=attributes or {}):
                return await func(*args, **kwargs)

        @wraps(func)
        def sync_wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
            tracer = trace.get_tracer(__name__)
            with tracer.start_as_current_span(span_name, attributes=attributes or {}):
                return func(*args, **kwargs)

        import asyncio

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator
