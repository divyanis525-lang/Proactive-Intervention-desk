"""
Life-Event Inference Agent.
Trigger: AGENT-DEPENDENT (PDF section 4.3) -- fires only after >= 2 swarm
agents have independently published *correlated* findings for the same
customer within a recency window. It never fires off a single raw event.

Toolset: cross-table joins across the swarm agents' published findings on
the StateBoard (structured conclusions only -- no raw reasoning access),
plus each customer's episodic memory for weighting ("has this pattern
happened before, and how did it resolve?").

Correlation rules are declarative below (`_RULES`): each rule lists the
StateBoard keys that must co-occur (from >=2 distinct agents) to support
an `inferred_state`. This is the "observation layer... filter that
matches the trigger to corresponding actions" called for in the task
brief -- implemented as a small rule table rather than free-form LLM
judgement, so the mapping from evidence -> inferred_state is itself
auditable and traceable.
"""
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

from agents.base_agent import BaseAgent
from memory.episodic_memory import EpisodicMemory

RECENCY_WINDOW_SECONDS = 60 * 60 * 24 * 21  # 3 weeks of simulated event time


@dataclass
class Rule:
    inferred_state: str
    required_keys: Set[str]       # at least these keys must all be present
    any_of_keys: Optional[Set[str]] = None  # plus at least one of these, if given
    base_confidence: str = "medium"


_RULES: List[Rule] = [
    Rule("new_child_life_event",
         required_keys={"healthcare_spend_signal"},
         any_of_keys={"childcare_interest", "new_dependent_signal"},
         base_confidence="high"),
    Rule("relocation",
         required_keys={"relocation_signal"},
         any_of_keys={"relocation_interest", "home_loan_interest"},
         base_confidence="high"),
    Rule("wealth_growth_or_windfall",
         required_keys={"large_inbound_deposit"},
         any_of_keys={"investment_interest", "home_loan_interest", "relocation_interest"},
         base_confidence="medium"),
    Rule("marriage_or_relationship_change",
         required_keys={"relationship_change_signal"},
         base_confidence="high"),
    Rule("job_loss_or_income_disruption",
         required_keys={"closure_intent_signal"},
         any_of_keys={"card_decline_signal", "sentiment"},
         base_confidence="medium"),
    Rule("financial_distress_general",
         required_keys={"transaction_anomaly_zscore"},
         any_of_keys={"card_decline_signal"},
         base_confidence="medium"),
    Rule("churn_risk",
         required_keys={"login_frequency_trend_pct"},
         any_of_keys={"closure_intent_signal", "sentiment", "urgency_flag"},
         base_confidence="medium"),
    Rule("retirement_transition",
         required_keys={"search_topic_signal"},
         base_confidence="low"),
]


class LifeEventAgent(BaseAgent):
    name = "life_event_agent"
    trigger_type = "agent_dependent"

    def __init__(self, state_board, episodic: EpisodicMemory):
        super().__init__(state_board)
        self.episodic = episodic

    def maybe_fire(self, customer_id: str, event: dict) -> Optional[Dict]:
        """Call after every swarm finding. Only actually 'fires' (produces
        a candidate life-event + confidence) if the agent-dependent
        condition -- correlated findings from >=2 agents -- is met."""

        def _run(span):
            board = self.state_board.board_for(customer_id)
            snap = board.snapshot()
            now = event.get("_sim_time", time.time())

            # index: key -> (agent, value, confidence, ts) using most recent, recent-enough finding
            recent: Dict[str, dict] = {}
            contributing_agents: Set[str] = set()
            for agent, findings in snap.items():
                for f in findings:
                    if now - f["ts"] > RECENCY_WINDOW_SECONDS:
                        continue
                    prev = recent.get(f["key"])
                    if prev is None or f["ts"] > prev["ts"]:
                        recent[f["key"]] = {**f, "agent": agent}

            best = None
            for rule in _RULES:
                if not rule.required_keys.issubset(recent.keys()):
                    continue
                if rule.any_of_keys and not (rule.any_of_keys & recent.keys()):
                    continue
                agents_involved = {recent[k]["agent"] for k in rule.required_keys}
                if rule.any_of_keys:
                    agents_involved |= {recent[k]["agent"] for k in (rule.any_of_keys & recent.keys())}
                if len(agents_involved) < 2:
                    continue  # agent-dependent trigger requires >= 2 independent agents to agree

                conf_rank = {"low": 1, "medium": 2, "high": 3}
                confidences = [recent[k]["confidence"] for k in rule.required_keys
                               if k in recent] + [
                    recent[k]["confidence"] for k in (rule.any_of_keys or set()) if k in recent
                ]
                strongest = max(confidences, key=lambda c: conf_rank[c])
                band = strongest if conf_rank[strongest] >= conf_rank[rule.base_confidence] else rule.base_confidence

                candidate = {
                    "inferred_state": rule.inferred_state,
                    "confidence_band": band,
                    "evidence_keys": sorted(recent.keys() & (rule.required_keys | (rule.any_of_keys or set()))),
                    "contributing_agents": sorted(agents_involved),
                    "source_event_ids": sorted({eid for k in recent if k in (rule.required_keys | (rule.any_of_keys or set()))
                                                 for eid in recent[k].get("source_event_ids", [])}),
                }
                if best is None or conf_rank[candidate["confidence_band"]] > conf_rank[best["confidence_band"]]:
                    best = candidate

            if best is None:
                return None

            # weight against episodic history (does this customer have a
            # relevant precedent that should raise/lower confidence?)
            ep = self.episodic.for_customer(customer_id)
            history = ep.relevant_history(best["inferred_state"])
            if history:
                best["episodic_precedent_count"] = len(history)

            self.publish(customer_id, "inferred_life_event_candidate", best["inferred_state"],
                         best["confidence_band"], self.trace_id(), best["source_event_ids"])
            span.set_attribute("finding.inferred_state", best["inferred_state"])
            span.set_attribute("finding.confidence_band", best["confidence_band"])
            return best

        return self.traced_run("life_event_agent.maybe_fire", _run, customer_id=customer_id)
