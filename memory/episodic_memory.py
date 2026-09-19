"""
Episodic memory (long-term, per-customer) + Semantic/shared memory
(PDF section 4.1).

Design decisions (research_log.md item R3), answering the brief's three
open design questions directly:

1. "When is a past episodic memory relevant enough to retrieve?"
   -> We tag every episodic entry with a `life_phase` and a `decayed`
      flag rather than doing similarity search over free text. An entry
      is retrieved only if it shares the *current* candidate life_phase
      or intervention category with the new signal (categorical match),
      not fuzzy semantic similarity -- this avoids retrieving irrelevant
      noise from an unrelated past incident, at the cost of missing
      truly novel analogies (documented as a limitation).

2. "How do we prevent memory leakage between customers at scale?"
   -> One JSON object per customer_id on disk (data/episodic/<id>.json),
      loaded/written only through EpisodicMemory.for_customer(id), which
      is the single chokepoint (mirrors StateBoard's design). No shared
      process-wide cache keyed by anything other than customer_id.

3. "Who is responsible for memory decay/expiry?"
   -> Explicit half-life policy: a flag's `weight` decays exponentially
      with age (see DECAY_HALF_LIFE_DAYS). A churn-risk flag from 18
      months ago is *not deleted* (kept for audit) but its weight when
      read by downstream agents (e.g. Retention Agent) is negligible
      unless explicitly reinforced by a more recent, similar flag.
"""
import json
import math
import os
import time
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional

DECAY_HALF_LIFE_DAYS = 90.0  # a flag loses half its weight every ~3 months


@dataclass
class EpisodicEntry:
    ts: float
    kind: str  # "life_phase_inference" | "intervention" | "outcome"
    life_phase: Optional[str]
    detail: Dict[str, Any]
    trace_id: Optional[str] = None

    def weight_now(self) -> float:
        age_days = (time.time() - self.ts) / 86400.0
        return 0.5 ** (age_days / DECAY_HALF_LIFE_DAYS)


class CustomerEpisodicMemory:
    def __init__(self, customer_id: str, store_dir: str):
        self.customer_id = customer_id
        self._path = os.path.join(store_dir, f"{customer_id}.json")
        self.entries: List[EpisodicEntry] = []
        self.current_life_phase: Optional[str] = None
        self.current_life_phase_confidence: float = 0.0
        self._load()

    def _load(self):
        if os.path.exists(self._path):
            with open(self._path) as f:
                data = json.load(f)
            self.entries = [EpisodicEntry(**e) for e in data.get("entries", [])]
            self.current_life_phase = data.get("current_life_phase")
            self.current_life_phase_confidence = data.get("current_life_phase_confidence", 0.0)

    def _save(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w") as f:
            json.dump({
                "entries": [asdict(e) for e in self.entries],
                "current_life_phase": self.current_life_phase,
                "current_life_phase_confidence": self.current_life_phase_confidence,
            }, f, indent=2)

    def set_life_phase(self, life_phase: str, confidence: float, trace_id: Optional[str] = None):
        self.current_life_phase = life_phase
        self.current_life_phase_confidence = confidence
        self.entries.append(EpisodicEntry(
            ts=time.time(), kind="life_phase_inference", life_phase=life_phase,
            detail={"confidence": confidence}, trace_id=trace_id,
        ))
        self._save()

    def record_intervention(self, action: str, outcome: Optional[str], trace_id: Optional[str] = None):
        self.entries.append(EpisodicEntry(
            ts=time.time(), kind="intervention", life_phase=self.current_life_phase,
            detail={"action": action, "outcome": outcome}, trace_id=trace_id,
        ))
        self._save()

    def relevant_history(self, candidate_life_phase: str, min_weight: float = 0.05) -> List[Dict[str, Any]]:
        """Categorical retrieval, per design decision (1) above."""
        out = []
        for e in self.entries:
            w = e.weight_now()
            if w < min_weight:
                continue
            if e.life_phase == candidate_life_phase or e.kind == "intervention":
                out.append({**asdict(e), "current_weight": round(w, 3)})
        return out


class EpisodicMemory:
    def __init__(self, store_dir: str = "data/episodic"):
        self.store_dir = store_dir
        self._cache: Dict[str, CustomerEpisodicMemory] = {}

    def for_customer(self, customer_id: str) -> CustomerEpisodicMemory:
        if customer_id not in self._cache:
            self._cache[customer_id] = CustomerEpisodicMemory(customer_id, self.store_dir)
        return self._cache[customer_id]


# ---------------------------------------------------------------------------
# Semantic / shared memory: cross-customer policy (offer eligibility,
# compliance rules). Not per-customer -- a single shared knowledge base
# that Offer/Eligibility and Critique-Refiner agents read from.
# ---------------------------------------------------------------------------

DEFAULT_POLICY = {
    "offer_eligibility": {
        "childcare_savings_or_insurance_offer": {"min_relationship_months": 3, "max_value_usd": 200},
        "mortgage_preapproval_offer": {"min_relationship_months": 12, "max_value_usd": 0,
                                        "requires_hitl": True, "notes": "unsolicited financial pitch tied to a sensitive personal inference -> always HITL"},
        "retention_credit": {"min_relationship_months": 1, "max_value_usd": 100,
                              "hitl_threshold_usd": 50},
        "higher_credit_line_offer": {"min_relationship_months": 6, "max_value_usd": 0, "requires_hitl": True},
    },
    "compliance": {
        "hard_stop_categories": ["legal_threat", "fraud", "self_harm_risk"],
        "max_autonomous_action_value_usd": 50,
    },
}


class SemanticMemory:
    """Cross-customer, cross-agent shared policy store."""

    def __init__(self, path: str = "data/policy.json"):
        self.path = path
        if os.path.exists(path):
            with open(path) as f:
                self.policy = json.load(f)
        else:
            self.policy = DEFAULT_POLICY
            os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
            with open(path, "w") as f:
                json.dump(self.policy, f, indent=2)

    def eligibility_rule(self, offer_key: str) -> Optional[Dict[str, Any]]:
        return self.policy.get("offer_eligibility", {}).get(offer_key)

    def max_autonomous_value(self) -> float:
        return self.policy.get("compliance", {}).get("max_autonomous_action_value_usd", 50)
