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

import logging
import sys
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog
import structlog.stdlib

# Third-party loggers we only want at WARNING+. Their DEBUG/INFO is chatty
# noise (HTTP request traces, SQL/checkpoint dumps, SDK internals) that would
# drown our own middleware decision logs in the file sink. Our own code (the
# ``src`` tree) is exempt -- see configure_logging().
_NOISY_LIBS: tuple[str, ...] = (
    "httpx",
    "httpcore",
    "urllib3",
    "openai",
    "sqlalchemy",
    "aiosqlite",
    "langchain",
    "langchain_core",
    "langgraph",
    "dashscope",
    "asyncio",
)

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
    log_file_path: str | None = None,
    log_file_max_bytes: int = 10 * 1024 * 1024,
    log_file_backup_count: int = 5,
) -> None:
    """Configure structlog with a console sink and an optional JSONL file sink.

    Uses structlog's ``ProcessorFormatter`` so that BOTH structlog events AND
    stdlib ``logging`` records (e.g. Langfuse/OTel internals that use
    ``logging.getLogger``) flow through the same processors and sinks -- this
    unifies the two previously-split log streams and guarantees every line
    carries the bound ``trace_id``/``session_id`` contextvars.

    Args:
        json_format: Console renderer -- ``True`` for JSON, ``False`` for
            human-readable colorized console. The file sink is always JSONL.
        min_level: Minimum log level for OUR code (0=DEBUG, 10=INFO, 20=WARNING,
            30=ERROR). 0 means "all levels" for the ``src`` tree. Third-party
            libraries default to INFO (WARNING+ for known-chatty ones).
        log_file_path: If set, mirror all logs to this JSONL file with rotation.
            Relative paths resolve against CWD (uvicorn runs from
            ``doctor/backend``). ``None`` -> stdout only (used by tests).
        log_file_max_bytes: Rotate the file sink once it exceeds this size.
        log_file_backup_count: Number of rotated ``.1``/``.2``/... files kept.
    """
    # Processors shared by structlog events and stdlib foreign records. Run
    # before the renderer so trace_id/session_id (from contextvars) and the
    # ISO timestamp land on every line, regardless of origin.
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _inject_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            # Hand the prepared event dict to stdlib logging; the
            # ProcessorFormatter on each handler does the final render.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(min_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # ── stdlib root logger: single source of handler attachment ──────────
    # Clear any prior handlers so repeated configure() calls (tests, reload)
    # don't stack duplicates.
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()

    # Level policy: log DEBUG+ from OUR code (the ``src`` tree + structlog
    # events, e.g. middleware decision logs like tool_call_skipped_duplicate),
    # but only INFO+ from third-party libraries by default and WARNING+ from
    # known-chatty ones. root stays at INFO so library DEBUG (httpx traces,
    # sqlalchemy checkpoint dumps, langchain internals) is dropped; the
    # ``src`` logger is pinned to app_level so our own debug logs survive.
    app_level = min_level if min_level > 0 else logging.DEBUG
    root.setLevel(max(app_level, logging.INFO))
    logging.getLogger("src").setLevel(app_level)
    for _lib in _NOISY_LIBS:
        logging.getLogger(_lib).setLevel(logging.WARNING)

    console_renderer = (
        structlog.processors.JSONRenderer()
        if json_format
        else structlog.dev.ConsoleRenderer()
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                # Strip the _record/_from_structlog meta that wrap_for_formatter
                # added (structlog 26.x no longer auto-injects this).
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                console_renderer,
            ],
        )
    )
    root.addHandler(console_handler)

    # ── Optional JSONL file sink (rotation) ──────────────────────────────
    # Observability must never block a diagnosis: if the file can't be opened
    # (permissions, bad path), fall back to console-only with a stderr notice.
    if log_file_path:
        try:
            log_path = Path(log_file_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_path,
                maxBytes=log_file_max_bytes,
                backupCount=log_file_backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(
                structlog.stdlib.ProcessorFormatter(
                    foreign_pre_chain=shared_processors,
                    processors=[
                        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                        structlog.processors.JSONRenderer(),
                    ],
                )
            )
            root.addHandler(file_handler)
        except Exception:
            sys.stderr.write(
                f"[logger] file sink disabled ({log_file_path!r} unavailable); "
                "console-only logging in effect.\n"
            )


def get_logger(name: str | None = None) -> structlog.BoundLogger:
    """Get a structlog BoundLogger with the given name.

    Args:
        name: Logger name (defaults to the calling module's __name__).

    Returns:
        A structlog BoundLogger instance.
    """
    return structlog.get_logger(name or __name__)  # type: ignore[no-any-return]
