"""OpenTelemetry initialization and observability sub-modules."""

from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

if TYPE_CHECKING:
    from fastapi import FastAPI


def init_observability(service_name: str = "doctor-api") -> None:
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


def instrument_fastapi(app: "FastAPI") -> None:
    """Instrument a FastAPI app for OpenTelemetry tracing."""
    FastAPIInstrumentor.instrument_app(app)
