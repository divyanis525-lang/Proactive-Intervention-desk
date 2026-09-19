"""
Critique/Compliance-Refiner Agent.
Trigger: agent-dependent (fires immediately after Retention Agent drafts
a proposal).
Toolset: SemanticMemory (policy RAG), guardrail rule checker, cost
estimator.

Coordination pattern: Critique-Refiner (PDF 4.2) -- this agent doesn't
re-decide the action, it *reviews* the Retention Agent's draft against
cost-feasibility and compliance rules and either passes it through,
downgrades it, or forces HITL escalation. This is the "final quality/
compliance pass before anything reaches HITL" the brief calls for.
"""
from typing import Dict

from agents.base_agent import BaseAgent
from memory.episodic_memory import SemanticMemory


class CritiqueAgent(BaseAgent):
    name = "critique_agent"
    trigger_type = "agent_dependent"

    def __init__(self, state_board, semantic: SemanticMemory):
        super().__init__(state_board)
        self.semantic = semantic

    def review(self, customer_id: str, draft: Dict, offer_proposal: Dict = None) -> Dict:
        def _run(span):
            action = draft["action"]
            requires_hitl = False
            reasons = []

            if action == "COMPLIANCE_FRAUD_HOLD":
                requires_hitl = True
                reasons.append("compliance/fraud actions always require human sign-off")

            if action == "PERSONALIZED_OFFER" and offer_proposal:
                value = offer_proposal.get("max_value_usd", 0)
                if offer_proposal.get("requires_hitl"):
                    requires_hitl = True
                    reasons.append(offer_proposal.get("notes") or "policy marks this offer type as HITL-required")
                threshold = offer_proposal.get("hitl_threshold_usd")
                if threshold is not None and value > threshold:
                    requires_hitl = True
                    reasons.append(f"offer value ${value} exceeds autonomous threshold ${threshold}")
                max_autonomous = self.semantic.max_autonomous_value()
                if value > max_autonomous:
                    requires_hitl = True
                    reasons.append(f"offer value ${value} exceeds global autonomous cap ${max_autonomous}")

            if action == "RELATIONSHIP_MANAGER_ESCALATION":
                requires_hitl = True
                reasons.append("RM escalation is a human-initiated action by definition")

            if draft.get("confidence_band") == "low" and action != "NO_ACTION":
                requires_hitl = True
                reasons.append("low confidence inference paired with a non-trivial action -> escalate for ambiguity")

            verdict = {
                **draft,
                "requires_hitl": requires_hitl,
                "critique_reasons": reasons,
                "critique_trace_id": self.trace_id(),
            }
            span.set_attribute("critique.requires_hitl", requires_hitl)
            self.publish(customer_id, "critique_verdict", "requires_hitl" if requires_hitl else "auto_ok",
                         "high", self.trace_id())
            return verdict

        return self.traced_run("critique_agent.review", _run, customer_id=customer_id, action=draft["action"])
