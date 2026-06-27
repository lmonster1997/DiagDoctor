"""OpenTelemetry initialization — tracing only."""

from typing import Any

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import get_current_span

# Needed by the monkey-patch below
from starlette.routing import Match, Route  # noqa: E402  # isort:skip


# ── Monkey-patch: fix _IncludedRouter AttributeError ─────────────────────────
# opentelemetry-instrumentation-fastapi 0.63b1 的 _get_route_details 在
# Match.PARTIAL 分支直接访问 starlette_route.path，但 starlette 内部的
# _IncludedRouter 没有 path 属性（app.include_router() 时产生）。
# 此补丁为 PARTIAL 分支补上 try/except 保护。
def _patch_otel_fastapi() -> None:
    try:
        import opentelemetry.instrumentation.fastapi as otel_fastapi

        def _patched_get_route_details(scope: dict[str, Any]) -> str:
            app = scope.get("app")
            if app is None or not hasattr(app, "routes"):
                return ""
            for starlette_route in app.routes:
                match, _ = (
                    Route.matches(starlette_route, scope)
                    if isinstance(starlette_route, Route)
                    else starlette_route.matches(scope)
                )
                if match == Match.FULL:
                    try:
                        return starlette_route.path  # type: ignore[no-any-return]
                    except AttributeError:
                        return scope.get("path", "")  # type: ignore[no-any-return]
                if match == Match.PARTIAL:
                    try:
                        return starlette_route.path  # type: ignore[no-any-return]
                    except AttributeError:
                        return scope.get("path", "")  # type: ignore[no-any-return]
            return ""

        otel_fastapi._get_route_details = _patched_get_route_details
    except Exception:
        pass  # 内部 API 变化时静默跳过, 不影响应用启动


_patch_otel_fastapi()
# ──────────────────────────────────────────────────────────────────────────────


def init_observability(service_name: str = "demo-backend") -> None:
    """
    Initialize OpenTelemetry tracing.

    Configures the TracerProvider with OTLP exporter, reading the endpoint
    from the OTEL_EXPORTER_OTLP_ENDPOINT environment variable.
    """
    resource = Resource.create({SERVICE_NAME: service_name})

    provider = TracerProvider(resource=resource)

    otlp_exporter = OTLPSpanExporter()
    provider.add_span_processor(BatchSpanProcessor(otlp_exporter))

    trace.set_tracer_provider(provider)


def setup_loki_logging(service_name: str = "demo-backend") -> None:
    """
    Bridge Python standard logging → Loki via HTTP Push API.

    Uses a lightweight custom handler that sends log records to Loki
    directly, bypassing the OTel Collector for logs (which is focused on traces).

    The Loki URL can be overridden via the ``LOKI_URL`` environment variable
    (defaults to ``http://localhost:3100`` for local dev outside Docker;
    use ``http://loki:3100`` when running inside Docker Compose).
    """
    import atexit
    import logging
    import os
    import queue
    import threading
    import time
    from contextlib import suppress

    import requests

    loki_url = os.getenv("LOKI_URL", "http://localhost:3100").rstrip("/") + "/loki/api/v1/push"
    _log_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=10000)
    _shutdown = threading.Event()

    def _otel_trace_context() -> tuple[str, str]:
        """Return (trace_id, span_id) from the current OTel span context.

        Returns empty strings when no active span exists (e.g. startup /
        background threads outside request handling).
        """
        span = get_current_span()
        ctx = span.get_span_context()
        if ctx.is_valid:
            # Format as 32-char / 16-char hex strings (standard OTel format)
            tid = format(ctx.trace_id, "032x")
            sid = format(ctx.span_id, "016x")
            return tid, sid
        return "", ""

    class _LokiHandler(logging.Handler):
        """Push log records to a queue; a background thread sends them to Loki.

        Each log entry carries the current OTel trace context (trace_id /
        span_id) so that downstream consumers (Doctor ingest, Grafana) can
        correlate logs with distributed traces.
        """

        def emit(self, record: logging.LogRecord) -> None:
            try:
                # Inject OTel trace context into Loki stream labels
                stream_labels = {
                    "service_name": service_name,
                    "level": record.levelname.lower(),
                }
                tid, sid = _otel_trace_context()
                if tid:
                    stream_labels["trace_id"] = tid
                if sid:
                    stream_labels["span_id"] = sid

                entry = {
                    "stream": stream_labels,
                    "values": [[str(int(record.created * 1_000_000_000)), self.format(record)]],
                }
                _log_queue.put_nowait(entry)
            except queue.Full:
                pass  # drop under extreme load

    def _sender() -> None:
        """Background thread: batch and send logs to Loki."""
        session = requests.Session()
        batch: list[dict[str, Any]] = []
        last_send = time.monotonic()

        while not _shutdown.is_set():
            try:
                item = _log_queue.get(timeout=1.0)
                batch.append(item)
            except queue.Empty:
                pass

            elapsed = time.monotonic() - last_send
            if batch and (len(batch) >= 50 or elapsed >= 5.0):
                payload = {"streams": batch}
                with suppress(Exception):
                    session.post(loki_url, json=payload, timeout=5)
                batch.clear()
                last_send = time.monotonic()

        # Flush remaining
        if batch:
            with suppress(Exception):
                session.post(loki_url, json={"streams": batch}, timeout=5)

    handler = _LokiHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)

    thread = threading.Thread(target=_sender, daemon=True, name="loki-sender")
    thread.start()
    atexit.register(_shutdown.set)


def instrument_fastapi(app: FastAPI) -> None:
    """Instrument a FastAPI app for OpenTelemetry tracing."""
    FastAPIInstrumentor.instrument_app(app)


def instrument_sqlalchemy() -> None:
    """Instrument SQLAlchemy engine for OpenTelemetry tracing.

    ``enable_commenter=True`` injects trace-context comments into SQL
    statements (``/* traceparent='...' */``), allowing the OTel SDK to
    create child spans for every SQL execution — critical for N+1
    detection and slow-query attribution in distributed traces.
    """
    SQLAlchemyInstrumentor().instrument(
        enable_commenter=True,
        commenter_options={},
    )
