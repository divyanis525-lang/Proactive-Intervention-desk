"""
Support/Sentiment Agent (swarm layer).
Trigger: event-based (new ticket or call transcript).
Toolset: lightweight sentiment classifier (lexicon-based, deterministic --
no external API needed) + urgency keyword scanner + ticket-history RAG.

Note: the hard-stop guardrail scan (legal/fraud/self-harm) happens
upstream in agents/guardrail_agent.py BEFORE this agent ever sees the
event, per the "independent of the rest of the pipeline" trigger design.
This agent handles ordinary sentiment/urgency, not hard-stops.
"""
from typing import Dict

from agents.base_agent import BaseAgent
from memory.vector_rag import LiveTfidfIndex

_NEGATIVE_WORDS = {
    "angry", "furious", "frustrated", "disappointed", "terrible", "awful",
    "worst", "unacceptable", "broken", "failed", "failure", "delay", "delayed",
    "waiting", "ignored", "rude", "useless", "horrible", "cancel", "refund",
}
_POSITIVE_WORDS = {
    "thanks", "thank", "great", "excellent", "resolved", "happy", "appreciate",
    "helpful", "quick", "good", "love", "perfect",
}
_URGENCY_WORDS = {"immediately", "urgent", "asap", "now", "today", "emergency"}


class SupportAgent(BaseAgent):
    name = "support_agent"
    allowed_source_systems = ["support_logs"]
    trigger_type = "event_based"

    def __init__(self, state_board, rag_index: LiveTfidfIndex):
        super().__init__(state_board)
        self.rag = rag_index

    def on_event(self, event: dict):
        self._check_scope(event["source_system"])

        def _run(span):
            cid = event["customer_id"]
            payload = event.get("payload", {})
            raw_text = payload.get("raw_text", "") or ""
            masked_text = self.safe_log_payload({"raw_text": raw_text})["raw_text"]

            # index into live RAG immediately -- no full re-index (checklist 6.1)
            self.rag.add(event["event_id"], masked_text,
                         metadata={"customer_id": cid, "type": "support_ticket"})

            sentiment, score = self._sentiment(raw_text)
            urgency = self._urgency(raw_text)

            self.publish(cid, "sentiment", sentiment,
                         "high" if abs(score) >= 2 else "medium",
                         self.trace_id(), [event["event_id"]])
            if urgency:
                self.publish(cid, "urgency_flag", True, "medium", self.trace_id(), [event["event_id"]])

            if event.get("event_type") == "ticket_resolved":
                res = payload.get("resolution_status")
                if res:
                    self.publish(cid, "last_ticket_resolution", res, "high",
                                 self.trace_id(), [event["event_id"]])

            span.set_attribute("finding.sentiment", sentiment)
            span.set_attribute("finding.urgency", urgency)
            return None

        self.traced_run("support_agent.on_event", _run, event_id=event.get("event_id"),
                         customer_id=event.get("customer_id"))

    @staticmethod
    def _sentiment(text: str):
        words = text.lower().split()
        score = sum(1 for w in words if w.strip(".,!?") in _POSITIVE_WORDS) - \
                sum(1 for w in words if w.strip(".,!?") in _NEGATIVE_WORDS)
        if score <= -1:
            return "negative", score
        if score >= 1:
            return "positive", score
        return "neutral", score

    @staticmethod
    def _urgency(text: str) -> bool:
        words = set(w.strip(".,!?").lower() for w in text.split())
        return bool(words & _URGENCY_WORDS)
