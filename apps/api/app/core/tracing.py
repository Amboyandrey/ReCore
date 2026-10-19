"""OpenTelemetry tracing setup — a generation span with one child per provider round trip and tool call.

Spans export over OTLP/HTTP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (see
infra/docker-compose.tracing.yml for a local Jaeger), and to stdout otherwise, so
`docker compose logs api` still shows a trace of one generation with no collector running.
"""

import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor

_configured = False


def configure_tracing(service_name: str) -> None:
    """Wire a process-wide TracerProvider exporting spans over OTLP or to stdout — idempotent.

    The stdout fallback uses `SimpleSpanProcessor`, exporting as each span ends, not the batching
    processor: a background export thread outliving pytest's per-test stdout capture is exactly
    the kind of "harmless in prod, noisy in tests" trap not worth the batching win at this volume.
    """
    global _configured
    if _configured:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        # The exporter reads the endpoint and headers from the standard OTEL_* env vars itself.
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    else:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True


def get_tracer(name: str) -> trace.Tracer:
    """Return a tracer bound to the given module name — the usual `__name__` at each call site."""
    return trace.get_tracer(name)
