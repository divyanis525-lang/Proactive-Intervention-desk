"""
Deterministic hard-stop guardrail (Production Bar Checklist 6.4, PDF section 2).

This is a plain keyword/regex scanner -- NOT an LLM call -- precisely so it
cannot be reasoned around by the model. It runs as an event-based trigger,
independent of the rest of the pipeline (see agents/guardrail_agent.py),
on every support_logs / social_signal_consented event the instant it
arrives, before the swarm layer even sees it.

Hit -> the event's customer is immediately routed to
COMPLIANCE_FRAUD_HOLD / a legal-escalation queue, and normal swarm/
synthesis/offer/retention processing for that specific event is skipped.
This models the brief's "furious legally-threatening email" situation.
"""
import re
from dataclasses import dataclass
from typing import List, Optional

LEGAL_KEYWORDS = [
    "lawsuit", "legal action", "sue you", "suing", "attorney", "my lawyer",
    "class action", "cease and desist", "litigation",
]

FRAUD_KEYWORDS = [
    "fraud", "fraudulent", "unauthorized transaction", "unauthorized charge",
    "someone stole", "account was hacked", "identity theft", "not my transaction",
    "didn't authorize", "stolen card", "account takeover",
]

# Self-harm / crisis language: routed to a human support escalation queue,
# never autonomously "handled" or offered a discount for.
SELF_HARM_KEYWORDS = [
    "kill myself", "want to die", "end my life", "suicide", "self harm", "self-harm",
]

_ALL_HARD_STOP = {
    "legal_threat": LEGAL_KEYWORDS,
    "fraud": FRAUD_KEYWORDS,
    "self_harm_risk": SELF_HARM_KEYWORDS,
}

_COMPILED = {
    category: [re.compile(re.escape(kw), re.IGNORECASE) for kw in kws]
    for category, kws in _ALL_HARD_STOP.items()
}


@dataclass
class GuardrailHit:
    category: str
    matched_keyword: str


def scan(text: Optional[str]) -> List[GuardrailHit]:
    """Deterministic scan. Returns [] if clean, else every hard-stop hit."""
    if not text:
        return []
    hits = []
    for category, patterns in _COMPILED.items():
        for pattern in patterns:
            if pattern.search(text):
                hits.append(GuardrailHit(category=category, matched_keyword=pattern.pattern))
    return hits


def route_for(hits: List[GuardrailHit]) -> str:
    """Deterministic routing table: category -> forced action.
    This mapping is itself the guardrail -- an agent cannot override it."""
    categories = {h.category for h in hits}
    if "self_harm_risk" in categories:
        return "SUPPORT_SERVICE_INTERVENTION"  # human specialist queue, not autonomous
    if "fraud" in categories:
        return "COMPLIANCE_FRAUD_HOLD"
    if "legal_threat" in categories:
        return "RELATIONSHIP_MANAGER_ESCALATION"
    return "NO_ACTION"
