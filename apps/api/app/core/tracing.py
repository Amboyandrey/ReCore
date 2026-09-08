"""OpenTelemetry tracing setup — one span per generation's provider call.

No collector is wired up yet (that's a hardening-phase concern, alongside a real metrics/traces
backend); spans export to stdout for now, so `docker compose logs api` shows a trace of exactly
what ARCHITECTURE.md's phase-7 demo asks for: "a trace of one generation."
"""

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

_configured = False


def configure_tracing(service_name: str) -> None:
    """Wire a process-wide TracerProvider exporting spans to stdout — idempotent, once per process.

    `SimpleSpanProcessor` exports synchronously as each span ends, deliberately not the batching
    processor: a background export thread outliving pytest's per-test stdout capture is exactly
    the kind of "harmless in prod, noisy in tests" trap not worth the batching win at this volume.
    """
    global _configured
    if _configured:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True


def get_tracer(name: str) -> trace.Tracer:
    """Return a tracer bound to the given module name — the usual `__name__` at each call site."""
    return trace.get_tracer(name)
