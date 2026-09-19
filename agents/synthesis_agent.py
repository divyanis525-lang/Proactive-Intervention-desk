"""
Synthesis/Correlation Agent.
Trigger: agent-dependent (fires once the Life-Event Agent has published a
candidate to the state board).
Toolset: state-board read, episodic + semantic memory read.

Coordination pattern used here: a lightweight "Agent Debate" resolution
(PDF section 4.2) when the swarm layer disagrees -- e.g. Transaction Agent
flags financial_distress-style anomalies while Usage Agent's search
history suggests wealth_growth. Rather than silently picking one, we
apply an explicit, documented priority rule (compliance/risk states
always win over opportunity states) and record *both* candidates plus
the resolution reason on the board, so the disagreement stays visible
in the trace instead of being averaged away.
"""
from typing import Dict, List, Optional

from agents.base_agent import BaseAgent
from memory.episodic_memory import EpisodicMemory, SemanticMemory

# Priority order when multiple inferred_states are plausible at once.
# Risk/compliance-flavoured states always outrank opportunity states --
# this is the explicit "who breaks the tie" rule required by 4.2's
# Agent Debate pattern.
_STATE_PRIORITY = [
    "potential_fraud_or_takeover",
    "elder_vulnerability_or_scam_risk",
    "medical_hardship",
    "job_loss_or_income_disruption",
    "financial_distress_general",
    "churn_risk",
    "new_child_life_event",
    "marriage_or_relationship_change",
    "relocation",
    "job_change_or_promotion",
    "retirement_transition",
    "wealth_growth_or_windfall",
    "small_business_cashflow_event",
    "no_significant_event",
]


class SynthesisAgent(BaseAgent):
    name = "synthesis_agent"
    trigger_type = "agent_dependent"

    def __init__(self, state_board, episodic: EpisodicMemory, semantic: SemanticMemory):
        super().__init__(state_board)
        self.episodic = episodic
        self.semantic = semantic

    def synthesize(self, customer_id: str, candidates: List[Dict]) -> Dict:
        def _run(span):
            if not candidates:
                result = {
                    "customer_id": customer_id,
                    "inferred_state": "no_significant_event",
                    "confidence_band": "low",
                    "resolution": "no_candidates",
                    "citations": [],
                }
            elif len(candidates) == 1:
                c = candidates[0]
                result = {
                    "customer_id": customer_id,
                    "inferred_state": c["inferred_state"],
                    "confidence_band": c["confidence_band"],
                    "resolution": "single_candidate",
                    "citations": c.get("source_event_ids", []),
                }
            else:
                # Agent Debate resolution: explicit priority order, disagreement recorded.
                ranked = sorted(candidates,
                                 key=lambda c: _STATE_PRIORITY.index(c["inferred_state"])
                                 if c["inferred_state"] in _STATE_PRIORITY else len(_STATE_PRIORITY))
                winner = ranked[0]
                result = {
                    "customer_id": customer_id,
                    "inferred_state": winner["inferred_state"],
                    "confidence_band": winner["confidence_band"],
                    "resolution": "agent_debate_priority_rule",
                    "debated_candidates": [c["inferred_state"] for c in candidates],
                    "citations": winner.get("source_event_ids", []),
                }

            # Persist to episodic memory: this becomes the customer's
            # standing "current life phase" that Offer/Retention agents
            # read from weeks later, per PDF section 3's standing goal.
            ep = self.episodic.for_customer(customer_id)
            conf_val = {"low": 0.3, "medium": 0.6, "high": 0.9}[result["confidence_band"]]
            if result["inferred_state"] != "no_significant_event":
                ep.set_life_phase(result["inferred_state"], conf_val, self.trace_id())

            self.publish(customer_id, "synthesized_state", result["inferred_state"],
                         result["confidence_band"], self.trace_id(), result["citations"])
            span.set_attribute("finding.synthesized_state", result["inferred_state"])
            span.set_attribute("finding.resolution", result["resolution"])
            return result

        return self.traced_run("synthesis_agent.synthesize", _run, customer_id=customer_id)
