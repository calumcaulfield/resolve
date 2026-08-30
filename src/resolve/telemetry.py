"""OpenTelemetry traces and Prometheus metrics.

The metrics chosen are the ones an operator would actually page on:
autonomous-resolution rate, escalation rate, spend, and — the one that matters
most for an AI system — the groundedness-failure counter. A rise there means
the agent is starting to make claims it cannot support, which is the failure
mode that reaches customers.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

from resolve.config import ObservabilitySettings
from resolve.logging import get_logger

log = get_logger(__name__)

TICKETS_PROCESSED = Counter(
    "resolve_tickets_processed_total",
    "Tickets processed by the agent",
    ["status", "intent"],
)
AGENT_COST = Counter("resolve_agent_cost_usd_total", "Model spend in USD", ["provider", "model"])
AGENT_DURATION = Histogram(
    "resolve_agent_duration_seconds",
    "End-to-end agent latency per ticket",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)
TOOL_CALLS = Counter("resolve_tool_calls_total", "Tool invocations", ["tool", "outcome", "risk"])
GROUNDEDNESS_FAILURES = Counter(
    "resolve_groundedness_failures_total",
    "Drafted replies rejected by the verifier",
    ["intent"],
)
APPROVALS_PENDING = Gauge(
    "resolve_approvals_pending", "Approval requests awaiting a human decision"
)
QUEUE_DEPTH = Gauge("resolve_queue_depth", "Unconsumed messages per topic", ["topic"])
OUTBOX_PENDING = Gauge("resolve_outbox_pending", "Undelivered outbound webhooks")
LLM_CACHE_HIT_RATE = Gauge("resolve_llm_cache_hit_rate", "Response cache hit rate")

_tracer: Any = None


def configure_telemetry(settings: ObservabilitySettings) -> None:
    """Wire up OTLP tracing. A no-op unless explicitly enabled."""
    global _tracer
    if not settings.enabled:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:  # pragma: no cover - optional
        log.warning("telemetry.unavailable", reason="opentelemetry packages not installed")
        return

    provider = TracerProvider(resource=Resource.create({"service.name": settings.service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.endpoint)))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(settings.service_name)
    log.info("telemetry.configured", endpoint=settings.endpoint)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[None]:
    """Start a span if tracing is on, otherwise do nothing at all."""
    if _tracer is None:
        yield
        return
    with _tracer.start_as_current_span(name) as active:
        for key, value in attributes.items():
            active.set_attribute(key, value)
        yield


def record_outcome(outcome: Any, provider: str) -> None:
    """Fold one agent outcome into the metric set."""
    intent = outcome.intent.value if outcome.intent else "unknown"
    TICKETS_PROCESSED.labels(status=outcome.status.value, intent=intent).inc()
    AGENT_DURATION.observe(outcome.duration_ms / 1000)
    AGENT_COST.labels(provider=provider, model="aggregate").inc(outcome.total_cost_usd)
    for result in outcome.tool_results:
        TOOL_CALLS.labels(
            tool=result.tool,
            outcome="ok" if result.ok else "error",
            risk=result.risk.value,
        ).inc()
    if outcome.verification is not None and not outcome.verification.grounded:
        GROUNDEDNESS_FAILURES.labels(intent=intent).inc()
