"""
Transaction/Billing Agent (swarm layer).
Trigger: event-based (real-time transaction stream).
Toolset: running per-customer baseline (mean/std of transaction amounts) +
statistical anomaly scoring (z-score) computed incrementally on the live
stream (checklist 6.1: "transforms applied on the live stream, not just
at query time").
"""
import math
from typing import Dict

from agents.base_agent import BaseAgent

LARGE_DEPOSIT_ABS_THRESHOLD = 5000.0


class RunningStats:
    """Welford's online algorithm -- mean/variance updated per event, O(1)."""

    def __init__(self):
        self.n = 0
        self.mean = 0.0
        self.m2 = 0.0

    def update(self, x: float):
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        delta2 = x - self.mean
        self.m2 += delta * delta2

    @property
    def std(self):
        if self.n < 2:
            return 0.0
        return math.sqrt(self.m2 / (self.n - 1))

    def zscore(self, x: float) -> float:
        if self.n < 2 or self.std == 0:
            return 0.0
        return (x - self.mean) / self.std


class TransactionAgent(BaseAgent):
    name = "transaction_agent"
    allowed_source_systems = ["card_payments", "instant_payments", "ach_wire", "core_banking_ledger"]
    trigger_type = "event_based"

    def __init__(self, state_board):
        super().__init__(state_board)
        self._stats: Dict[str, RunningStats] = {}

    def on_event(self, event: dict):
        self._check_scope(event["source_system"])

        def _run(span):
            cid = event["customer_id"]
            payload = event.get("payload", {})
            amount = payload.get("amount")
            if amount is None:
                return None
            amount = float(amount)
            stats = self._stats.setdefault(cid, RunningStats())
            z = stats.zscore(amount)
            stats.update(amount)

            if abs(z) >= 2.5 and stats.n > 5:
                conf = "high" if abs(z) >= 4 else "medium"
                self.publish(cid, "transaction_anomaly_zscore", round(z, 2), conf,
                             self.trace_id(), [event["event_id"]])
                span.set_attribute("finding.zscore", z)

            direction = payload.get("direction")
            etype = event.get("event_type")
            is_inbound_large = (
                (direction == "inbound_transfer" and amount >= LARGE_DEPOSIT_ABS_THRESHOLD)
                or (etype == "deposit" and amount >= LARGE_DEPOSIT_ABS_THRESHOLD)
            )
            if is_inbound_large:
                self.publish(cid, "large_inbound_deposit", amount, "high",
                             self.trace_id(), [event["event_id"]])

            if etype == "decline" or payload.get("decline_reason"):
                self.publish(cid, "card_decline_signal", payload.get("decline_reason", "unknown"),
                             "medium", self.trace_id(), [event["event_id"]])

            if payload.get("mcc_category") in ("hospital", "pharmacy", "medical_services"):
                self.publish(cid, "healthcare_spend_signal", True, "low",
                             self.trace_id(), [event["event_id"]])
            return None

        self.traced_run("transaction_agent.on_event", _run, event_id=event.get("event_id"),
                         customer_id=event.get("customer_id"))
