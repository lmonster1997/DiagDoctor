"""
Tests for src.observability.logger — structured logging with structlog.
"""

from src.observability.logger import (
    bind_log_context,
    clear_log_context,
    configure_logging,
    get_logger,
    session_id_ctx,
    trace_id_ctx,
)


class TestConfigureLogging:
    """Tests for logging configuration."""

    def test_configure_json_format(self) -> None:
        """JSON format should configure without error."""
        configure_logging(json_format=True)
        logger = get_logger("test.json")
        assert logger is not None

    def test_configure_console_format(self) -> None:
        """Console (dev) format should configure without error."""
        configure_logging(json_format=False)
        logger = get_logger("test.console")
        assert logger is not None


class TestGetLogger:
    """Tests for get_logger."""

    def test_get_logger_with_name(self) -> None:
        """Logger with explicit name should work."""
        configure_logging(json_format=False)
        log = get_logger("my.module")
        assert log is not None
        # Should be bindable
        bound = log.bind(foo="bar")
        assert bound is not None

    def test_get_logger_default_name(self) -> None:
        """Logger without name should default to __name__."""
        configure_logging(json_format=False)
        log = get_logger()
        assert log is not None


class TestContextBinding:
    """Tests for trace_id / session_id contextvars binding."""

    def test_bind_trace_id(self) -> None:
        """bind_log_context should set trace_id_ctx."""
        bind_log_context(trace_id="trace-abc")
        assert trace_id_ctx.get() == "trace-abc"

    def test_bind_session_id(self) -> None:
        """bind_log_context should set session_id_ctx."""
        bind_log_context(session_id="sess-xyz")
        assert session_id_ctx.get() == "sess-xyz"

    def test_bind_both_ids(self) -> None:
        """bind_log_context should set both trace and session IDs."""
        bind_log_context(trace_id="t1", session_id="s1")
        assert trace_id_ctx.get() == "t1"
        assert session_id_ctx.get() == "s1"

    def test_bind_extra_kwargs(self) -> None:
        """Extra kwargs should be bound to structlog context."""
        bind_log_context(trace_id="t2", session_id="s2", request_id="r-99")
        assert trace_id_ctx.get() == "t2"
        assert session_id_ctx.get() == "s2"
        # structlog contextvars binding should have occurred

    def test_bind_empty_context(self) -> None:
        """bind_log_context with no args should not raise."""
        bind_log_context()
        # Should not throw

    def test_clear_log_context(self) -> None:
        """clear_log_context should reset trace_id and session_id."""
        bind_log_context(trace_id="clear-me", session_id="clear-too")
        clear_log_context()
        assert trace_id_ctx.get() == ""
        assert session_id_ctx.get() == ""


class TestLoggerOutput:
    """Integration-style tests for logger output."""

    def test_logger_info_emits(self, capsys) -> None:  # type: ignore[no-untyped-def]
        """Logger.info should write to stdout."""
        configure_logging(json_format=True)
        bind_log_context(trace_id="logtest-1", session_id="logtest-sess")
        logger = get_logger("test.output")
        logger.info("hello world")

        captured = capsys.readouterr()
        assert "hello world" in captured.err or "hello world" in captured.out

    def test_logger_context_in_output(self, capsys) -> None:  # type: ignore[no-untyped-def]
        """Trace ID should appear in JSON log output."""
        configure_logging(json_format=True)
        bind_log_context(trace_id="ctx-test-42")
        logger = get_logger("test.ctx")
        logger.info("contextual")

        captured = capsys.readouterr()
        output = captured.err + captured.out
        assert "ctx-test-42" in output


class TestInjectContextVarsProcessor:
    """Direct tests for the _inject_contextvars processor."""

    def test_injects_trace_id(self) -> None:
        """Processor should inject trace_id from contextvar."""
        from src.observability.logger import _inject_contextvars

        trace_id_ctx.set("proc-trace")
        session_id_ctx.set("")
        event = _inject_contextvars(None, "info", {"event": "test"})  # type: ignore[arg-type]
        assert event["trace_id"] == "proc-trace"

    def test_injects_session_id(self) -> None:
        """Processor should inject session_id from contextvar."""
        from src.observability.logger import _inject_contextvars

        trace_id_ctx.set("")
        session_id_ctx.set("proc-sess")
        event = _inject_contextvars(None, "info", {"event": "test"})  # type: ignore[arg-type]
        assert event["session_id"] == "proc-sess"

    def test_no_inject_when_empty(self) -> None:
        """Processor should not add keys when contextvars are empty."""
        from src.observability.logger import _inject_contextvars

        trace_id_ctx.set("")
        session_id_ctx.set("")
        event = _inject_contextvars(None, "info", {"event": "test"})  # type: ignore[arg-type]
        assert "trace_id" not in event
        assert "session_id" not in event

    def test_preserves_existing_trace_id(self) -> None:
        """Processor should not overwrite an already-present trace_id."""
        from src.observability.logger import _inject_contextvars

        trace_id_ctx.set("ctx-value")
        event = _inject_contextvars(None, "info", {"event": "test", "trace_id": "existing"})  # type: ignore[arg-type]
        assert event["trace_id"] == "existing"  # Not overwritten
