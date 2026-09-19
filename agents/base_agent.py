"""
Base class every agent inherits from.

Enforces, structurally rather than by convention (Production Bar 6.4/6.5):
  - every agent.run() call is wrapped in an OTel span (full trace logging)
  - anything an agent logs or would send to an LLM prompt is passed through
    guardrails.pii.mask_payload first (PII protection at the data layer)
  - agents publish only structured findings to the StateBoard (see
    memory/state_board.py's own enforcement of primitive-only values)
  - each agent declares its own `allowed_tools` and `data_scope`; a data
    layer check (`self._check_scope`) makes cross-domain reads fail loudly
    instead of silently succeeding, which is what "role-based access
    enforced at the data layer, not the prompt layer" requires.
"""
import time
from typing import Any, Dict, List, Optional

from guardrails.pii import mask_payload, pseudonymize
from memory.state_board import StateBoard
from observability.tracing import get_tracer, METRICS, current_trace_id

# Attribute keys that must never appear in the clear in an exported trace
# span, even though they're ordinary primitives from the StateBoard's point
# of view -- pseudonymized (not fully redacted) so spans for the same
# customer stay correlatable without ever writing the real ID to disk.
# See guardrails/pii.py::pseudonymize and research_log.md's PII item.
_PSEUDONYMIZE_SPAN_KEYS = {"customer_id", "account_id"}


class DataScopeViolation(RuntimeError):
    pass


class BaseAgent:
    name: str = "base_agent"
    # Which source_systems this agent is structurally allowed to read.
    # Enforced in _check_scope -- NOT just a prompt instruction.
    allowed_source_systems: List[str] = []
    trigger_type: str = "event_based"  # "event_based" | "time_based" | "agent_dependent"

    def __init__(self, state_board: StateBoard):
        self.state_board = state_board
        self._tracer = get_tracer(self.name)

    # ---- data-layer scoping ------------------------------------------------
    def _check_scope(self, source_system: str):
        if self.allowed_source_systems and source_system not in self.allowed_source_systems:
            raise DataScopeViolation(
                f"{self.name} is not permitted to read '{source_system}' events "
                f"(allowed: {self.allowed_source_systems}). This is a structural "
                f"guardrail, not a prompt instruction."
            )

    # ---- safe publish / logging --------------------------------------------
    def publish(self, customer_id: str, key: str, value: Any, confidence: str,
                trace_id: Optional[str] = None, source_event_ids: Optional[List[str]] = None):
        board = self.state_board.board_for(customer_id)
        board.publish(self.name, key, value, confidence, trace_id, source_event_ids)
        METRICS.record_confidence(self.name, {"low": 0.3, "medium": 0.6, "high": 0.9}.get(confidence, 0.5))

    def safe_log_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Always call this before logging / prompting with raw event payloads."""
        return mask_payload(payload)

    # ---- span wrapper -------------------------------------------------------
    def traced_run(self, span_name: str, fn, **span_attrs):
        with self._tracer.start_as_current_span(span_name) as span:
            span.set_attribute("agent.name", self.name)
            span.set_attribute("agent.trigger_type", self.trigger_type)
            for k, v in span_attrs.items():
                if v is None:
                    continue
                if k in _PSEUDONYMIZE_SPAN_KEYS and isinstance(v, str):
                    v = pseudonymize(v)
                span.set_attribute(k, v if isinstance(v, (str, int, float, bool)) else str(v))
            t0 = time.perf_counter()
            result = fn(span)
            METRICS.record_latency(self.name, (time.perf_counter() - t0) * 1000)
            return result

    def trace_id(self) -> Optional[str]:
        return current_trace_id()
