"""
Observability layer: OpenTelemetry tracing + structured JSONL trace log.

Design decision (see research_log.md, item R1):
We use OpenTelemetry's SDK with a custom SpanExporter that writes one JSON
line per finished span to data/traces.jsonl, instead of shipping to a real
OTLP collector. This keeps the demo runnable offline while still using the
real OTel API surface (tracer.start_as_current_span, span attributes,
span events) so trace_id/span_id propagation, parent/child relationships,
and per-stage latency are all genuinely captured, not hand-rolled.

Every agent invocation, tool call, retrieval and handoff becomes a span.
The trace_id of the root span for a customer-event's processing is used
as the "trace id" referenced throughout the assignment brief (agent
outputs, HITL prompts, inferred_events.json citations).
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Optional

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider, ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

_LOCK = threading.Lock()


class JsonlFileSpanExporter(SpanExporter):
    """Writes each finished span as one JSON line -> full trace/audit log."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # truncate at start of each run so logs correspond to this run only
        open(self.path, "w").close()

    def export(self, spans) -> SpanExportResult:
        with _LOCK, open(self.path, "a") as f:
            for span in spans:
                f.write(json.dumps(self._span_to_dict(span)) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass

    @staticmethod
    def _span_to_dict(span: ReadableSpan) -> Dict[str, Any]:
        ctx = span.get_span_context()
        parent = span.parent
        return {
            "trace_id": format(ctx.trace_id, "032x"),
            "span_id": format(ctx.span_id, "016x"),
            "parent_span_id": format(parent.span_id, "016x") if parent else None,
            "name": span.name,
            "start_time_ns": span.start_time,
            "end_time_ns": span.end_time,
            "duration_ms": round((span.end_time - span.start_time) / 1e6, 3) if span.end_time else None,
            "attributes": dict(span.attributes or {}),
            "events": [
                {"name": e.name, "attributes": dict(e.attributes or {}), "timestamp_ns": e.timestamp}
                for e in span.events
            ],
            "status": span.status.status_code.name,
        }


_provider: Optional[TracerProvider] = None


def init_tracing(trace_log_path: str = "data/traces.jsonl", service_name: str = "customer360-mas"):
    """Idempotent setup. Call once from main.py before any agent runs."""
    global _provider
    if _provider is not None:
        return _provider
    resource = Resource.create({"service.name": service_name})
    _provider = TracerProvider(resource=resource)
    _provider.add_span_processor(SimpleSpanProcessor(JsonlFileSpanExporter(trace_log_path)))
    trace.set_tracer_provider(_provider)
    return _provider


def get_tracer(name: str):
    return trace.get_tracer(name)


def current_trace_id() -> Optional[str]:
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx is None or ctx.trace_id == 0:
        return None
    return format(ctx.trace_id, "032x")


class Metrics:
    """Minimal in-memory metrics counter (latency, confidence, HITL rates).

    A real deployment would push these to Prometheus/OTel metrics; for this
    assignment we keep an in-process registry and dump it to
    data/metrics.json at the end of the run (see main.py:_dump_metrics).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.stage_latencies_ms: Dict[str, list] = {}
        self.agent_confidences: Dict[str, list] = {}
        self.hitl_counts: Dict[str, int] = {}
        self.action_counts: Dict[str, int] = {}
        self.guardrail_trips: int = 0

    def record_latency(self, stage: str, ms: float):
        with self._lock:
            self.stage_latencies_ms.setdefault(stage, []).append(ms)

    def record_confidence(self, agent: str, score: float):
        with self._lock:
            self.agent_confidences.setdefault(agent, []).append(score)

    def record_hitl(self, status: str):
        with self._lock:
            self.hitl_counts[status] = self.hitl_counts.get(status, 0) + 1

    def record_action(self, action: str):
        with self._lock:
            self.action_counts[action] = self.action_counts.get(action, 0) + 1

    def record_guardrail_trip(self):
        with self._lock:
            self.guardrail_trips += 1

    def snapshot(self) -> Dict[str, Any]:
        def avg(xs):
            return round(sum(xs) / len(xs), 2) if xs else None

        return {
            "stage_latency_ms_avg": {k: avg(v) for k, v in self.stage_latencies_ms.items()},
            "agent_confidence_avg": {k: avg(v) for k, v in self.agent_confidences.items()},
            "hitl_counts": self.hitl_counts,
            "action_counts": self.action_counts,
            "guardrail_trips": self.guardrail_trips,
        }

    def dump(self, path: str = "data/metrics.json"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.snapshot(), f, indent=2)


METRICS = Metrics()


class Timer:
    """Context manager: records wall-clock latency of a stage into METRICS."""

    def __init__(self, stage: str):
        self.stage = stage
        self._t0 = None

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        ms = (time.perf_counter() - self._t0) * 1000
        METRICS.record_latency(self.stage, ms)
        return False
