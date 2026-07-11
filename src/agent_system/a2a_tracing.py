import base64

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter as OTLPHttpSpanExporter,
)
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .settings import (
    LANGFUSE_BASE_URL,
    LANGFUSE_PUBLIC_KEY,
    LANGFUSE_SECRET_KEY,
    OTEL_EXPORTER_OTLP_ENDPOINT,
)

_TRACING_INITIALIZED = False


def init_tracing(service_name: str = "agent_system_a2a", also_export_to_langfuse: bool = False) -> None:
    global _TRACING_INITIALIZED
    if _TRACING_INITIALIZED:
        return

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    # Jaeger's all-in-one image accepts OTLP/gRPC natively on 4317 (the
    # dedicated Jaeger exporter/protocol has been removed upstream).
    otlp_exporter = OTLPSpanExporter(endpoint=OTEL_EXPORTER_OTLP_ENDPOINT, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(otlp_exporter))

    if also_export_to_langfuse and LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY:
        # Second, independent destination for the same spans - e.g. the
        # orchestrator's ADK spans, which already carry gen_ai.* semantic
        # convention content (prompts/completions) natively, no extra
        # instrumentation needed. Langfuse's OTLP endpoint only accepts
        # HTTP (not gRPC), and uses Basic Auth with the project keys.
        auth = base64.b64encode(f"{LANGFUSE_PUBLIC_KEY}:{LANGFUSE_SECRET_KEY}".encode()).decode()
        langfuse_exporter = OTLPHttpSpanExporter(
            endpoint=f"{LANGFUSE_BASE_URL}/api/public/otel/v1/traces",
            headers={
                "Authorization": f"Basic {auth}",
                "x-langfuse-ingestion-version": "4",
            },
        )
        provider.add_span_processor(BatchSpanProcessor(langfuse_exporter))

    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    _TRACING_INITIALIZED = True


def instrument_app(app) -> None:
    FastAPIInstrumentor.instrument_app(app)
