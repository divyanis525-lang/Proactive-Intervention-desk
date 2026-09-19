"""
Shared per-customer State Board (PDF section 4.1: "Working memory").

Design decision (research_log.md item R2): agents never get raw access to
another agent's reasoning trace -- only the *structured conclusion* it
published (e.g. {"login_frequency_trend_pct": -40, "confidence": "high"}).
This is enforced here structurally: publish() only accepts a flat dict of
primitives (no nested chain-of-thought blobs), and read_all() returns a
frozen snapshot copy so a downstream agent can't mutate another agent's
finding.

Scoping: one board per customer_id, held in a dict keyed by customer_id,
so there is no cross-customer bleed-through by construction (a bug in
`board_for()` would need to hand out the wrong customer's board object,
which is a single, auditable chokepoint -- see PII/scoping note below).
"""
import copy
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Finding:
    agent: str
    key: str
    value: Any
    confidence: str  # "low" | "medium" | "high"
    trace_id: Optional[str]
    ts: float = field(default_factory=time.time)
    source_event_ids: List[str] = field(default_factory=list)


class CustomerBoard:
    def __init__(self, customer_id: str):
        self.customer_id = customer_id
        self._findings: Dict[str, List[Finding]] = {}  # agent -> findings (history kept, latest wins on read)
        self._lock = threading.Lock()

    def publish(self, agent: str, key: str, value: Any, confidence: str,
                trace_id: Optional[str] = None, source_event_ids: Optional[List[str]] = None):
        if not isinstance(value, (str, int, float, bool, type(None))):
            raise TypeError(
                f"StateBoard.publish only accepts primitive values (got {type(value)} for "
                f"{agent}.{key}) -- publish structured conclusions, not raw reasoning traces."
            )
        f = Finding(agent=agent, key=key, value=value, confidence=confidence,
                    trace_id=trace_id, source_event_ids=source_event_ids or [])
        with self._lock:
            self._findings.setdefault(agent, []).append(f)

    def latest(self, agent: str, key: str) -> Optional[Finding]:
        with self._lock:
            for f in reversed(self._findings.get(agent, [])):
                if f.key == key:
                    return copy.deepcopy(f)
        return None

    def agents_with_findings_since(self, since_ts: float) -> List[str]:
        with self._lock:
            return [a for a, fs in self._findings.items() if any(f.ts >= since_ts for f in fs)]

    def snapshot(self) -> Dict[str, List[Dict[str, Any]]]:
        """Frozen, read-only view for Synthesis/downstream agents."""
        with self._lock:
            return {
                agent: [
                    {"key": f.key, "value": f.value, "confidence": f.confidence,
                     "trace_id": f.trace_id, "ts": f.ts, "source_event_ids": f.source_event_ids}
                    for f in findings
                ]
                for agent, findings in self._findings.items()
            }


class StateBoard:
    """Registry of per-customer boards. Scoping chokepoint: every lookup
    goes through board_for(customer_id) -- there is exactly one place in
    the codebase that maps customer_id -> board, which is what an auditor
    would review to confirm there's no cross-customer leakage."""

    def __init__(self):
        self._boards: Dict[str, CustomerBoard] = {}
        self._lock = threading.Lock()

    def board_for(self, customer_id: str) -> CustomerBoard:
        with self._lock:
            if customer_id not in self._boards:
                self._boards[customer_id] = CustomerBoard(customer_id)
            return self._boards[customer_id]
