"""
Usage/Engagement Agent (swarm layer).
Trigger: event-based (each web_app_events event) + time-based (daily rollup,
driven by main.py's scheduler calling `daily_rollup`).
Toolset: windowed aggregation over the event stream + simple trend detection.
"""
from collections import deque
from typing import Dict, List

from agents.base_agent import BaseAgent

WINDOW_EVENTS = 30  # rolling window size per customer for trend detection


class UsageAgent(BaseAgent):
    name = "usage_agent"
    allowed_source_systems = ["web_app_events"]
    trigger_type = "event_based+time_based"

    def __init__(self, state_board):
        super().__init__(state_board)
        self._history: Dict[str, deque] = {}
        self._search_flags: Dict[str, List[str]] = {}

    def on_event(self, event: dict):
        self._check_scope(event["source_system"])

        def _run(span):
            cid = event["customer_id"]
            payload = event.get("payload", {})
            hist = self._history.setdefault(cid, deque(maxlen=WINDOW_EVENTS))
            hist.append(event["event_time"])

            etype = event.get("event_type")
            if etype == "login":
                trend = self._login_trend(cid)
                if trend is not None:
                    conf = "high" if abs(trend) > 30 else "medium" if abs(trend) > 15 else "low"
                    self.publish(cid, "login_frequency_trend_pct", round(trend, 1), conf,
                                 self.trace_id(), [event["event_id"]])
                    span.set_attribute("finding.login_trend_pct", trend)

            if etype == "search_query":
                search_text = self.safe_log_payload(payload).get("search_text", "") or ""
                flagged = self._flag_search_topic(search_text)
                if flagged:
                    flags = self._search_flags.setdefault(cid, [])
                    flags.append(flagged)
                    self.publish(cid, "search_topic_signal", flagged, "medium",
                                 self.trace_id(), [event["event_id"]])
                    span.set_attribute("finding.search_topic", flagged)

            if etype == "feature_used":
                feat = payload.get("feature_or_page", "")
                if feat in ("account_closure", "cancel_subscription", "close_account"):
                    self.publish(cid, "closure_intent_signal", True, "high",
                                 self.trace_id(), [event["event_id"]])
            return None

        self.traced_run("usage_agent.on_event", _run, event_id=event.get("event_id"),
                         customer_id=event.get("customer_id"))

    def _login_trend(self, customer_id: str):
        hist = self._history.get(customer_id)
        if hist is None or len(hist) < 6:
            return None
        mid = len(hist) // 2
        first_half, second_half = list(hist)[:mid], list(hist)[mid:]
        if len(first_half) == 0:
            return None
        # crude "frequency" proxy: fewer logins in the recent half than the
        # earlier half of the observed window => negative trend
        rate_before = len(first_half)
        rate_after = len(second_half)
        if rate_before == 0:
            return None
        return ((rate_after - rate_before) / rate_before) * 100.0

    @staticmethod
    def _flag_search_topic(search_text: str) -> str:
        text = search_text.lower()
        topic_map = {
            "home loan": "home_loan_interest", "mortgage": "home_loan_interest",
            "loan": "loan_interest", "credit line": "credit_line_interest",
            "close account": "closure_intent", "cancel": "closure_intent",
            "baby": "childcare_interest", "childcare": "childcare_interest",
            "daycare": "childcare_interest", "divorce": "divorce_signal",
            "moving": "relocation_interest", "relocation": "relocation_interest",
            "retirement": "retirement_interest", "invest": "investment_interest",
        }
        for kw, label in topic_map.items():
            if kw in text:
                return label
        return ""
