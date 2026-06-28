from opentelemetry import trace
from opentelemetry.exporter.jaeger.proto.grpc import JaegerExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def init_tracing(service_name: str = "agent_system_a2a") -> None:
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    jaeger_exporter = JaegerExporter(endpoint="http://localhost:4317", insecure=True)
    provider.add_span_processor(BatchSpanProcessor(jaeger_exporter))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()


def instrument_app(app) -> None:
    FastAPIInstrumentor.instrument_app(app)
