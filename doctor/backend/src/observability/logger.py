"""
Structured logging via structlog.

Provides JSON-formatted logs with automatic trace_id and session_id
from contextvars. Use `bind_log_context` to attach trace/session ids
to all subsequent log messages within the current async context.

Usage:
    from src.observability.logger import get_logger, bind_log_context

    logger = get_logger(__name__)
    bind_log_context(trace_id="abc123", session_id="sess-1")
    logger.info("Processing request")  # includes trace_id, session_id
"""

from contextvars import ContextVar
from typing import Any

import structlog

# ── Context variables for cross-cutting log context ──────────────────

trace_id_ctx: ContextVar[str] = ContextVar("trace_id", default="")
session_id_ctx: ContextVar[str] = ContextVar("session_id", default="")


def _inject_contextvars(
    logger: structlog.types.WrappedLogger,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Processor: inject trace_id and session_id from contextvars into every log event."""
    trace_id = trace_id_ctx.get()
    session_id = session_id_ctx.get()
    if trace_id:
        event_dict.setdefault("trace_id", trace_id)
    if session_id:
        event_dict.setdefault("session_id", session_id)
    return event_dict


def bind_log_context(
    trace_id: str = "",
    session_id: str = "",
    **extra: str,
) -> None:
    """
    Bind trace/session IDs and extra key-value pairs to the current async context.

    These values will automatically appear in every log message produced
    within the same contextvars scope (e.g., the same request or session).

    Args:
        trace_id: OpenTelemetry trace ID.
        session_id: Identifier for the current diagnosis session.
        **extra: Additional key-value pairs to bind as log context.
    """
    if trace_id:
        trace_id_ctx.set(trace_id)
    if session_id:
        session_id_ctx.set(session_id)
    structlog.contextvars.bind_contextvars(**extra)


def clear_log_context() -> None:
    """Clear all bound context from structlog contextvars."""
    structlog.contextvars.clear_contextvars()
    trace_id_ctx.set("")
    session_id_ctx.set("")


def configure_logging(
    json_format: bool = True,
    min_level: int = 0,
) -> None:
    """
    Configure structlog for JSON output with timestamp in ISO format.

    Args:
        json_format: If True, output JSON. If False, use human-readable console output.
        min_level: Minimum log level to emit (0=DEBUG, 10=INFO, 20=WARNING, 30=ERROR).
    """
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _inject_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    if json_format:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(min_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.BoundLogger:
    """Get a structlog BoundLogger with the given name.

    Args:
        name: Logger name (defaults to the calling module's __name__).

    Returns:
        A structlog BoundLogger instance.
    """
    return structlog.get_logger(name or __name__)  # type: ignore[no-any-return]
