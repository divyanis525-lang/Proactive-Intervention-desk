"""
Escalation/Guardrail Agent.
Trigger: event-based, fires INSTANTLY on pattern match, independent of the
rest of the pipeline (PDF roster table + section 2's "Guardrails"
requirement). This agent runs BEFORE the swarm layer even sees a
support_logs / social_signal_consented event -- see main.py's ingestion
loop, which calls `GuardrailAgent.scan_event` first and only forwards the
event to the swarm if it comes back clean.

This wraps guardrails/keyword_scanner.py (deterministic, non-LLM) so the
hit/route decision is itself traced and auditable like every other agent
action, without giving an LLM any chance to reason around it.
"""
from typing import Dict, Optional

from agents.base_agent import BaseAgent
from guardrails.keyword_scanner import scan, route_for
from guardrails.pii import mask_text
from observability.tracing import METRICS

_TEXT_BEARING_SOURCES = {"support_logs", "social_signal_consented"}


class GuardrailAgent(BaseAgent):
    name = "guardrail_agent"
    trigger_type = "event_based"

    def scan_event(self, event: dict) -> Optional[Dict]:
        """Returns a hard-stop verdict dict if this event trips the
        guardrail, else None (pipeline proceeds normally)."""
        if event.get("source_system") not in _TEXT_BEARING_SOURCES:
            return None

        def _run(span):
            payload = event.get("payload", {})
            text = payload.get("raw_text") or payload.get("call_transcript") or ""
            hits = scan(text)
            if not hits:
                return None

            forced_action = route_for(hits)
            METRICS.record_guardrail_trip()
            verdict = {
                "customer_id": event["customer_id"],
                "action": forced_action,
                "action_subtype": "hard_stop_guardrail",
                "hitl_status": "escalated",
                "confidence_band": "high",
                "guardrail_hits": [{"category": h.category} for h in hits],  # no matched keyword text -> avoid re-exposing raw PII/content
                "citations": [event["event_id"]],
                "trace_id": self.trace_id(),
            }
            self.publish(event["customer_id"], "guardrail_hard_stop", forced_action, "high",
                         self.trace_id(), [event["event_id"]])
            span.set_attribute("guardrail.hit", True)
            span.set_attribute("guardrail.forced_action", forced_action)
            span.set_attribute("guardrail.masked_snippet", mask_text(text[:120]))
            return verdict

        return self.traced_run("guardrail_agent.scan_event", _run, event_id=event.get("event_id"),
                                customer_id=event.get("customer_id"))
