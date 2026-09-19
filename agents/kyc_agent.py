"""
KYC/Compliance Agent (swarm layer).
Trigger: event-based (KYC update) + time-based (periodic re-verification,
driven externally by main.py's scheduler).
Toolset: KYC field-change diff, sanctions/watchlist stub, compliance rule
lookups against SemanticMemory.
"""
from agents.base_agent import BaseAgent

_LIFE_EVENT_SUBTYPES = {
    "address_change": "relocation_signal",
    "marital_status_change": "relationship_change_signal",
    "dependents_change": "new_dependent_signal",
}


class KycAgent(BaseAgent):
    name = "kyc_agent"
    allowed_source_systems = ["loan_kyc"]
    trigger_type = "event_based+time_based"

    def on_event(self, event: dict):
        self._check_scope(event["source_system"])

        def _run(span):
            cid = event["customer_id"]
            payload = event.get("payload", {})
            subtype = payload.get("event_subtype") or event.get("event_type")

            if subtype in _LIFE_EVENT_SUBTYPES:
                self.publish(cid, _LIFE_EVENT_SUBTYPES[subtype],
                             self.safe_log_payload(payload).get("new_value", True),
                             "high", self.trace_id(), [event["event_id"]])
                span.set_attribute("finding.kyc_subtype", subtype)

            if event.get("event_type") == "loan_application":
                self.publish(cid, "loan_application_signal", True, "high",
                             self.trace_id(), [event["event_id"]])
            if event.get("event_type") == "loan_disbursed":
                self.publish(cid, "loan_disbursed_signal", True, "high",
                             self.trace_id(), [event["event_id"]])
            return None

        self.traced_run("kyc_agent.on_event", _run, event_id=event.get("event_id"),
                         customer_id=event.get("customer_id"))
