"""
Offer/Eligibility Agent.
Trigger: agent-dependent (fires after Synthesis Agent produces a candidate
opportunity state).
Toolset: SemanticMemory (policy/eligibility rules -- see
memory/episodic_memory.py::SemanticMemory) + a simple eligibility
calculator. This agent only *proposes*; it cannot send anything (checklist
6.4 "safe tool-calling boundaries" -- enforced by simply not giving this
class any send/apply method).
"""
from typing import Dict, Optional

from agents.base_agent import BaseAgent
from memory.episodic_memory import SemanticMemory

# Observation layer: maps an inferred_state to a candidate offer subtype,
# if any. This is the "filter that matches the trigger to corresponding
# actions" called for in the brief -- a small declarative table, not
# free-form LLM judgement, so it's auditable.
_STATE_TO_OFFER = {
    "new_child_life_event": "childcare_savings_or_insurance_offer",
    "relocation": "mortgage_preapproval_offer",
    "wealth_growth_or_windfall": "higher_credit_line_offer",
    "job_change_or_promotion": "higher_credit_line_offer",
}


class OfferAgent(BaseAgent):
    name = "offer_agent"
    trigger_type = "agent_dependent"

    def __init__(self, state_board, semantic: SemanticMemory):
        super().__init__(state_board)
        self.semantic = semantic

    def propose(self, customer_id: str, inferred_state: str, relationship_months: int = 12) -> Optional[Dict]:
        def _run(span):
            offer_key = _STATE_TO_OFFER.get(inferred_state)
            if offer_key is None:
                return None
            rule = self.semantic.eligibility_rule(offer_key)
            if rule is None:
                return None
            eligible = relationship_months >= rule.get("min_relationship_months", 0)
            proposal = {
                "offer_key": offer_key,
                "eligible": eligible,
                "max_value_usd": rule.get("max_value_usd", 0),
                "requires_hitl": rule.get("requires_hitl", False) or rule.get("hitl_threshold_usd", 0) > 0,
                "hitl_threshold_usd": rule.get("hitl_threshold_usd"),
                "notes": rule.get("notes"),
            }
            self.publish(customer_id, "offer_proposal", offer_key if eligible else f"ineligible:{offer_key}",
                         "high" if eligible else "low", self.trace_id())
            span.set_attribute("finding.offer_key", offer_key)
            span.set_attribute("finding.eligible", eligible)
            return proposal

        return self.traced_run("offer_agent.propose", _run, customer_id=customer_id,
                                inferred_state=inferred_state)
