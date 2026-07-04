import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_TRACING_INITIALIZED = False


def init_tracing(service_name: str = "agent_system_a2a") -> None:
    global _TRACING_INITIALIZED
    if _TRACING_INITIALIZED:
        return

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    # Jaeger's all-in-one image accepts OTLP/gRPC natively on 4317 (the
    # dedicated Jaeger exporter/protocol has been removed upstream).
    otlp_exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    _TRACING_INITIALIZED = True


def instrument_app(app) -> None:
    FastAPIInstrumentor.instrument_app(app)
