"""
Stream ingestion engine + agent orchestration pipeline.

Usage:
    python main.py --scenario_dir data/sample_scenario --hitl_mode auto
    python main.py --stream_file data/event_stream.json --hitl_mode interactive

Accepts EITHER:
  - a scenario directory matching the Customer 360 Dataset Schema (README):
      entities.json, history_seed.jsonl, live_stream.jsonl, replay_config.json
  - or a flat --stream_file of newline-delimited (or JSON-array) events in
    the same event envelope schema, for quick ad-hoc testing.

Coordination topology actually used (see research_log.md for the reasoning
behind picking this combination, and PDF 4.2 for the pattern definitions):
  1. HANDOFF:      ingestion -> swarm -> life-event -> synthesis -> offer
                    -> retention -> critique -> HITL  (linear backbone)
  2. SWARM:        Usage/Support/Transaction/KYC agents all run in
                    parallel against the same incoming event batch,
                    independently publishing to the StateBoard.
  3. AGENT-DEBATE: inside SynthesisAgent, when >1 life-event candidate
                    is plausible at once.
  4. CRITIQUE-REFINER: CritiqueAgent reviews RetentionAgent's draft before
                    HITL.
  5. Guardrail scan is a SEPARATE, event-based, always-first check that
                    can short-circuit the whole backbone above.

Out-of-order handling (checklist 6.1): events are sorted into a small
watermark buffer keyed by event_time (not ingestion_time) before being
fed to the swarm, so a late-arriving event doesn't get processed "in the
future" relative to events that logically preceded it. See `Watermark`.
"""
import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agents.usage_agent import UsageAgent
from agents.support_agent import SupportAgent
from agents.transaction_agent import TransactionAgent
from agents.kyc_agent import KycAgent
from agents.life_event_agent import LifeEventAgent
from agents.synthesis_agent import SynthesisAgent
from agents.offer_agent import OfferAgent
from agents.retention_agent import RetentionAgent
from agents.critique_agent import CritiqueAgent
from agents.guardrail_agent import GuardrailAgent
from agents.hitl import HitlGate
from memory.state_board import StateBoard
from memory.episodic_memory import EpisodicMemory, SemanticMemory
from memory.vector_rag import LiveTfidfIndex
from observability.tracing import init_tracing, get_tracer, METRICS
from guardrails.pii import pseudonymize

STATE_TO_ENUM_ACTION_ALREADY_MATCHES = True  # our internal action strings already match the required output enum


def _iso_to_epoch(iso_str: str) -> float:
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).timestamp()


class Watermark:
    """Small out-of-order buffer: holds events until `delay_seconds` of
    simulated event_time has passed with nothing earlier arriving, then
    releases them in event_time order. Prevents a late/out-of-order event
    from corrupting windowed aggregations (checklist 6.1)."""

    def __init__(self, delay_seconds: float = 5.0):
        self.delay = delay_seconds
        self._buffer: List[dict] = []
        self._max_seen_time = 0.0

    def add(self, event: dict) -> List[dict]:
        et = _iso_to_epoch(event["event_time"])
        event["_sim_time"] = et
        self._max_seen_time = max(self._max_seen_time, et)
        self._buffer.append(event)
        return self._drain()

    def _drain(self) -> List[dict]:
        ready = [e for e in self._buffer if e["_sim_time"] <= self._max_seen_time - self.delay]
        if not ready and self._buffer and len(self._buffer) > 500:
            # safety valve: never let the buffer grow unbounded on a short synthetic run
            ready = sorted(self._buffer, key=lambda e: e["_sim_time"])[:1]
        ready.sort(key=lambda e: e["_sim_time"])
        for e in ready:
            self._buffer.remove(e)
        return ready

    def flush(self) -> List[dict]:
        ready = sorted(self._buffer, key=lambda e: e["_sim_time"])
        self._buffer = []
        return ready


class Orchestrator:
    """The Blackboard pattern's Control Component (see research_log.md R9):
    the one place that decides which agent runs next based on the current
    contents of the shared StateBoard (the Blackboard), rather than each
    agent deciding for itself when to act."""

    def __init__(self, hitl_mode: str = "auto", relationship_months_default: int = 12,
                 live_output_path: Optional[str] = None):
        self.state_board = StateBoard()
        self.episodic = EpisodicMemory()
        self.semantic = SemanticMemory()
        self.rag = LiveTfidfIndex()

        self.guardrail_agent = GuardrailAgent(self.state_board)
        self.usage_agent = UsageAgent(self.state_board)
        self.support_agent = SupportAgent(self.state_board, self.rag)
        self.transaction_agent = TransactionAgent(self.state_board)
        self.kyc_agent = KycAgent(self.state_board)
        self.life_event_agent = LifeEventAgent(self.state_board, self.episodic)
        self.synthesis_agent = SynthesisAgent(self.state_board, self.episodic, self.semantic)
        self.offer_agent = OfferAgent(self.state_board, self.semantic)
        self.retention_agent = RetentionAgent(self.state_board, self.episodic)
        self.critique_agent = CritiqueAgent(self.state_board, self.semantic)
        self.hitl = HitlGate(mode=hitl_mode)

        self.relationship_months_default = relationship_months_default
        self.live_output_path = live_output_path
        self.checkpoints: List[Dict] = []
        self.tracer = get_tracer("orchestrator")

    # -------------------------------------------------------------------
    def _swarm_dispatch(self, event: dict):
        src = event["source_system"]
        if src == "web_app_events":
            self.usage_agent.on_event(event)
        elif src == "support_logs":
            self.support_agent.on_event(event)
        elif src in ("card_payments", "instant_payments", "ach_wire", "core_banking_ledger"):
            self.transaction_agent.on_event(event)
        elif src == "loan_kyc":
            self.kyc_agent.on_event(event)
        elif src == "trading_brokerage":
            pass  # out of scope for this roster; would get its own swarm agent in a fuller build
        elif src == "social_signal_consented":
            pass  # consumed for guardrail scanning only in this build

    def _emit_checkpoint(self, event: dict, customer_id: str, inferred_state: str,
                          confidence_band: str, action: str, action_subtype: Optional[str],
                          hitl_status: str, notes: str, citations: List[str]):
        cp = {
            "as_of_time": event["event_time"],
            "customer_id": customer_id,
            "inferred_state": inferred_state,
            "confidence_band": confidence_band,
            "action": action,
            "action_subtype": action_subtype,
            "hitl_status": hitl_status,
            "notes": notes,
            "citations": citations,
        }
        self.checkpoints.append(cp)
        if self.live_output_path:
            self._flush_live(self.live_output_path)

    def _flush_live(self, path: str):
        """Re-writes the full checkpoint array after every new checkpoint.
        Used in live-stream modes (stdin/tail/tcp) so a partial/interrupted
        run still leaves a valid, up-to-date inferred_events.json on disk
        instead of only writing once at the very end."""
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.checkpoints, f, indent=2)
        os.replace(tmp, path)

    def process_event(self, event: dict):
        with self.tracer.start_as_current_span("process_event") as span:
            span.set_attribute("event_id", event.get("event_id", ""))
            span.set_attribute("customer_id", pseudonymize(event.get("customer_id", "")))
            span.set_attribute("source_system", event.get("source_system", ""))
            cid = event["customer_id"]

            # 1) Guardrail scan -- event-based, independent, ALWAYS FIRST.
            hard_stop = self.guardrail_agent.scan_event(event)
            if hard_stop is not None:
                hard_stop["hitl_status"] = self.hitl.review({**hard_stop, "requires_hitl": True})["hitl_status"]
                self.episodic.for_customer(cid).record_intervention(
                    hard_stop["action"], hard_stop["hitl_status"], hard_stop.get("trace_id"))
                METRICS.record_action(hard_stop["action"])
                self._emit_checkpoint(
                    event, cid, "potential_fraud_or_takeover" if hard_stop["action"] == "COMPLIANCE_FRAUD_HOLD" else "elder_vulnerability_or_scam_risk",
                    "high", hard_stop["action"], hard_stop["action_subtype"], hard_stop["hitl_status"],
                    "Hard-stop guardrail keyword match -- routed straight to escalation, swarm/synthesis bypassed for this event.",
                    hard_stop["citations"],
                )
                return  # guardrail bypasses the rest of the pipeline for this event

            # 2) Swarm layer (parallel-independent, run sequentially here for
            #    a reproducible single-process demo; each agent is stateless
            #    across calls except for its own internal per-customer stats,
            #    so this is safe to parallelize with asyncio/threads later).
            self._swarm_dispatch(event)

            # 3) Agent-dependent trigger: Life-Event Inference Agent only
            #    fires if >=2 swarm agents have correlated findings.
            candidate = self.life_event_agent.maybe_fire(cid, event)
            if candidate is None:
                return  # not enough correlated signal yet -- nothing to synthesize

            # 4) Synthesis (agent-dependent on life-event candidate existing)
            synthesis_result = self.synthesis_agent.synthesize(cid, [candidate])
            if synthesis_result is None:
                return

            # 5) Offer/Eligibility (agent-dependent on synthesis producing
            #    an opportunity-flavoured state)
            offer_proposal = self.offer_agent.propose(
                cid, synthesis_result["inferred_state"], self.relationship_months_default)

            # 6) Retention/Action -- see agents/retention_agent.py module
            #    docstring for the exact trigger-point contract being
            #    enforced here (synthesis_result is guaranteed non-None).
            draft = self.retention_agent.decide(cid, synthesis_result, offer_proposal)

            # 7) Critique-Refiner pass
            verdict = self.critique_agent.review(cid, draft, offer_proposal)

            # 8) HITL gate
            verdict = self.hitl.review(verdict)
            METRICS.record_action(verdict["action"])

            self.episodic.for_customer(cid).record_intervention(
                verdict["action"], verdict["hitl_status"], verdict.get("trace_id"))

            notes = f"resolution={synthesis_result.get('resolution')}"
            if verdict.get("critique_reasons"):
                notes += f"; critique={verdict['critique_reasons']}"

            self._emit_checkpoint(
                event, cid, synthesis_result["inferred_state"], synthesis_result["confidence_band"],
                verdict["action"], verdict.get("action_subtype"), verdict["hitl_status"], notes,
                verdict.get("citations", []),
            )

    # -------------------------------------------------------------------
    def run_stream(self, events: Iterable[dict], watermark_delay: float = 5.0):
        wm = Watermark(delay_seconds=watermark_delay)
        for event in events:
            ready = wm.add(event)
            for e in ready:
                self.process_event(e)
        for e in wm.flush():
            self.process_event(e)

    def dump_outputs(self, out_path: str = "data/inferred_events.json"):
        os.makedirs(os.path.dirname(out_path), exist_ok=True) if os.path.dirname(out_path) else None
        with open(out_path, "w") as f:
            json.dump(self.checkpoints, f, indent=2)
        METRICS.dump()
        print(f"Wrote {len(self.checkpoints)} checkpoints -> {out_path}")


# --------------------------------------------------------------------------
def load_events_flat(path: str) -> List[dict]:
    events = []
    with open(path) as f:
        content = f.read().strip()
    if content.startswith("["):
        events = json.loads(content)
    else:
        for line in content.splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
    events.sort(key=lambda e: e["event_time"])
    return events


def load_scenario_dir(scenario_dir: str) -> List[dict]:
    events = []
    for fname in ("history_seed.jsonl", "live_stream.jsonl"):
        path = os.path.join(scenario_dir, fname)
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
    events.sort(key=lambda e: e["event_time"])
    return events


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stream_file", default=None, help="flat JSON/JSONL event stream file (batch)")
    parser.add_argument("--scenario_dir", default=None, help="dir with history_seed.jsonl + live_stream.jsonl (batch)")
    parser.add_argument("--stdin", action="store_true", help="LIVE: read newline-delimited JSON events from stdin as they arrive")
    parser.add_argument("--tail_file", default=None, help="LIVE: tail a file the replay tool is appending to")
    parser.add_argument("--idle_timeout_seconds", type=float, default=None,
                         help="LIVE (--tail_file only): stop after this many seconds with no new lines; omit to run forever")
    parser.add_argument("--tcp", default=None, help="LIVE: connect to host:port streaming newline-delimited JSON")
    parser.add_argument("--out", default="data/inferred_events.json")
    parser.add_argument("--hitl_mode", default="auto", choices=["auto", "interactive"])
    parser.add_argument("--watermark_delay_seconds", type=float, default=5.0)
    args = parser.parse_args()

    init_tracing()

    live_modes_requested = sum([args.stdin, bool(args.tail_file), bool(args.tcp)])
    if live_modes_requested > 1:
        print("ERROR: pass only one of --stdin / --tail_file / --tcp")
        sys.exit(1)

    if live_modes_requested == 1:
        # Genuinely live ingestion: events are pulled from the source in
        # true arrival order (not pre-sorted -- can't sort a source that
        # hasn't finished arriving). Checkpoints are flushed to --out
        # after every new one, and Ctrl-C still leaves a valid file.
        import ingestion_adapters as ia
        if args.stdin:
            print("LIVE mode: reading events from stdin...")
            event_iter = ia.iter_stdin_jsonl()
        elif args.tail_file:
            print(f"LIVE mode: tailing {args.tail_file} ...")
            event_iter = ia.iter_growing_file(args.tail_file, stop_after_idle=args.idle_timeout_seconds)
        else:
            host, port = args.tcp.split(":")
            print(f"LIVE mode: connecting to {host}:{port} ...")
            event_iter = ia.iter_tcp_jsonl(host, int(port))

        orch = Orchestrator(hitl_mode=args.hitl_mode, live_output_path=args.out)
        try:
            orch.run_stream(event_iter, watermark_delay=args.watermark_delay_seconds)
        except KeyboardInterrupt:
            print("\nInterrupted -- flushing final state.")
        orch.dump_outputs(args.out)
        return

    if not args.stream_file and not args.scenario_dir:
        default_dir = "data/sample_scenario"
        if os.path.isdir(default_dir):
            args.scenario_dir = default_dir
            print(f"No --stream_file/--scenario_dir given; defaulting to {default_dir}")
        else:
            print("ERROR: must pass --stream_file, --scenario_dir, or a live mode (--stdin/--tail_file/--tcp)")
            sys.exit(1)

    events = load_scenario_dir(args.scenario_dir) if args.scenario_dir else load_events_flat(args.stream_file)
    print(f"Loaded {len(events)} events. HITL mode: {args.hitl_mode}")

    orch = Orchestrator(hitl_mode=args.hitl_mode)
    t0 = time.time()
    orch.run_stream(events, watermark_delay=args.watermark_delay_seconds)
    print(f"Processed stream in {time.time() - t0:.2f}s")
    orch.dump_outputs(args.out)


if __name__ == "__main__":
    main()
