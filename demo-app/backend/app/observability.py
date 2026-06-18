"""
OpenTelemetry initialization.

IMPORTANT: This module must be imported BEFORE FastAPI app instantiation
to ensure instrumentation hooks are in place.
"""

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

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

        def _patched_get_route_details(scope: dict) -> str:
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
                        return starlette_route.path
                    except AttributeError:
                        return scope.get("path", "")
                if match == Match.PARTIAL:
                    try:
                        return starlette_route.path
                    except AttributeError:
                        return scope.get("path", "")
            return ""

        otel_fastapi._get_route_details = _patched_get_route_details  # type: ignore[attr-defined]
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


def instrument_fastapi(app) -> None:
    """Instrument a FastAPI app for OpenTelemetry tracing."""
    FastAPIInstrumentor.instrument_app(app)


def instrument_sqlalchemy() -> None:
    """Instrument SQLAlchemy engine for OpenTelemetry tracing."""
    SQLAlchemyInstrumentor().instrument()
