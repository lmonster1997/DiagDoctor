"""
Structured logging via structlog.

Provides JSON-formatted logs with automatic trace_id and session_id
from contextvars.
"""

import structlog

from contextvars import ContextVar

# Context variables for cross-cutting log context
trace_id_ctx: ContextVar[str] = ContextVar("trace_id", default="")
session_id_ctx: ContextVar[str] = ContextVar("session_id", default="")


def configure_logging() -> None:
    """Configure structlog for JSON output with timestamp in ISO format."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer()
            if False
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.BoundLogger:
    """Get a structlog logger with the given name."""
    return structlog.get_logger(name or __name__)
