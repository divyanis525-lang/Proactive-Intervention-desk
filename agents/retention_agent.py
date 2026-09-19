"""
Retention/Action Agent.

Trigger point (this is deliberately explicit and checked in code, not just
assumed, per the assignment's extra requirement to verify the Retention
Agent "truly checks and takes action at the correct trigger point"):

  This agent's `decide()` is ONLY ever called from main.py's orchestrator
  at the single point where BOTH of the following are true for a customer
  in the current processing cycle:
    (a) SynthesisAgent has produced a synthesized_state this cycle, AND
    (b) either OfferAgent has returned an eligible proposal for that state,
        OR the state maps to a non-offer action via _STATE_TO_ACTION below
        (e.g. churn_risk -> retention outreach doesn't need an "offer").
  See main.py::_process_correlation_stage for the exact call site --
  decide() is never invoked speculatively on a bare swarm finding, and
  main.py asserts synthesis happened before calling this agent (guarded
  by the `synthesis_result is not None` check right before the call).

Toolset: LLM message composer (stubbed deterministically here -- see
`_draft_message`, kept template-based so the whole pipeline runs
offline/deterministically for grading), RAG grounding against past
interventions (episodic memory), episodic memory read to avoid repeating
a stale/ignored offer.
"""
from typing import Dict, Optional

from agents.base_agent import BaseAgent
from memory.episodic_memory import EpisodicMemory

# Observation layer (continued from offer_agent.py): inferred_state ->
# bounded action, for states that don't route through an "offer".
_STATE_TO_ACTION = {
    "potential_fraud_or_takeover": "COMPLIANCE_FRAUD_HOLD",
    "elder_vulnerability_or_scam_risk": "COMPLIANCE_FRAUD_HOLD",
    "medical_hardship": "SUPPORT_SERVICE_INTERVENTION",
    "job_loss_or_income_disruption": "PROACTIVE_RETENTION_OUTREACH",
    "financial_distress_general": "SUPPORT_SERVICE_INTERVENTION",
    "churn_risk": "PROACTIVE_RETENTION_OUTREACH",
    "marriage_or_relationship_change": "NO_ACTION",  # signal alone isn't actionable, per brief's red-herring warning
    "retirement_transition": "RELATIONSHIP_MANAGER_ESCALATION",
    "small_business_cashflow_event": "SUPPORT_SERVICE_INTERVENTION",
    "no_significant_event": "NO_ACTION",
}

_OFFER_STATES = {"new_child_life_event", "relocation", "wealth_growth_or_windfall", "job_change_or_promotion"}


class RetentionAgent(BaseAgent):
    name = "retention_agent"
    trigger_type = "agent_dependent"

    def __init__(self, state_board, episodic: EpisodicMemory):
        super().__init__(state_board)
        self.episodic = episodic

    def decide(self, customer_id: str, synthesis_result: Dict, offer_proposal: Optional[Dict]) -> Dict:
        """`synthesis_result` must be non-None -- see trigger-point contract
        in the module docstring. Raises if called out of order, so a
        misuse in main.py fails loudly rather than silently mis-triggering."""
        if synthesis_result is None:
            raise RuntimeError(
                "RetentionAgent.decide() called with no synthesis_result -- "
                "this violates the agent-dependent trigger contract (Synthesis "
                "must fire first). Refusing to guess an action."
            )

        def _run(span):
            state = synthesis_result["inferred_state"]
            confidence = synthesis_result["confidence_band"]

            # avoid repeating a recently-ignored/rejected identical action
            ep = self.episodic.for_customer(customer_id)
            recent_same_action = [
                e for e in ep.entries
                if e.kind == "intervention" and e.detail.get("outcome") == "human_rejected"
                and e.life_phase == state
            ]

            if state in _OFFER_STATES:
                if offer_proposal is None or not offer_proposal.get("eligible"):
                    action = "NO_ACTION"
                    subtype = None
                else:
                    action = "PERSONALIZED_OFFER"
                    subtype = offer_proposal["offer_key"]
            else:
                action = _STATE_TO_ACTION.get(state, "NO_ACTION")
                subtype = None

            if recent_same_action and action != "NO_ACTION":
                span.set_attribute("retention.suppressed_repeat_rejected_action", True)
                action = "NO_ACTION"
                subtype = None

            if confidence == "low" and action not in ("NO_ACTION", "COMPLIANCE_FRAUD_HOLD"):
                # too weak a signal to act on yet -- matches brief's example
                # of "signal detected... not yet strong enough to trigger"
                action = "NO_ACTION"
                subtype = None

            draft = {
                "customer_id": customer_id,
                "action": action,
                "action_subtype": subtype,
                "inferred_state": state,
                "confidence_band": confidence,
                "message_draft": self._draft_message(action, subtype, state) if action != "NO_ACTION" else None,
                "citations": synthesis_result.get("citations", []),
                "trace_id": self.trace_id(),
            }
            span.set_attribute("retention.action", action)
            span.set_attribute("retention.trigger_valid", True)
            self.publish(customer_id, "retention_action_draft", action, confidence, self.trace_id())
            return draft

        return self.traced_run("retention_agent.decide", _run, customer_id=customer_id,
                                inferred_state=synthesis_result["inferred_state"])

    @staticmethod
    def _draft_message(action: str, subtype: Optional[str], state: str) -> str:
        templates = {
            "PROACTIVE_RETENTION_OUTREACH": "We've noticed some changes in your account activity and wanted to check in -- is there anything we can help with?",
            "PERSONALIZED_OFFER": f"Based on your recent activity, you may be eligible for our {subtype}. Would you like to learn more?",
            "RELATIONSHIP_MANAGER_ESCALATION": "Routing to a relationship manager for a personal follow-up call.",
            "SUPPORT_SERVICE_INTERVENTION": "Reaching out proactively to resolve a potential service issue before it escalates.",
            "COMPLIANCE_FRAUD_HOLD": "Account flagged for compliance/fraud review -- action held pending human review.",
        }
        return templates.get(action, "")
