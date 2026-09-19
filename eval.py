"""
Automated scoring harness (PDF section 8: brownie-points scoring harness;
task doc's `eval.py` requirement).

Scores an inferred_events / checkpoints output against a ground_truth.json
of the shape:
    [{"customer_id": ..., "expected_state": ..., "expected_action": ..., "by": <ISO time>}]

Reports, per the brief's three evaluation criteria:
  1. Accuracy of inference  -- did the system ever reach expected_state for
     that customer, and not get *stuck* on a wrong state at the end.
  2. Timeliness             -- did it reach a high-confidence, correct
     checkpoint at/around `by`, not too early (on weak signal) or missed
     entirely.
  3. Appropriate action     -- did the final action for that customer match
     expected_action.

Usage:
    python eval.py --inferred data/inferred_events.json \
                    --ground_truth data/sample_scenario/ground_truth.json
"""
import argparse
import json
from collections import defaultdict
from datetime import datetime
from typing import Dict, List


def _parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load(path: str):
    with open(path) as f:
        return json.load(f)


def group_by_customer(checkpoints: List[Dict]) -> Dict[str, List[Dict]]:
    out = defaultdict(list)
    for cp in checkpoints:
        out[cp["customer_id"]].append(cp)
    for cid in out:
        out[cid].sort(key=lambda c: c["as_of_time"])
    return out


def score(inferred_path: str, ground_truth_path: str, verbose: bool = True) -> Dict:
    checkpoints = load(inferred_path)
    ground_truth = load(ground_truth_path)
    by_customer = group_by_customer(checkpoints)

    results = []
    for gt in ground_truth:
        cid = gt["customer_id"]
        cps = by_customer.get(cid, [])
        state_hit = any(cp["inferred_state"] == gt["expected_state"] for cp in cps)
        action_hit = any(cp["action"] == gt["expected_action"] for cp in cps)

        timeliness = "n/a"
        if state_hit:
            by_time = _parse_time(gt["by"])
            hits = [cp for cp in cps if cp["inferred_state"] == gt["expected_state"]]
            first_hit_time = _parse_time(hits[0]["as_of_time"])
            delta_hours = (first_hit_time - by_time).total_seconds() / 3600.0
            if abs(delta_hours) <= 24:
                timeliness = "on_time"
            elif delta_hours > 24:
                timeliness = "late"
            else:
                timeliness = "early"

        final_action = cps[-1]["action"] if cps else None
        results.append({
            "customer_id": cid,
            "expected_state": gt["expected_state"],
            "expected_action": gt["expected_action"],
            "state_hit": state_hit,
            "action_hit": action_hit,
            "timeliness": timeliness,
            "final_action": final_action,
            "num_checkpoints": len(cps),
        })

    n = len(results) or 1
    summary = {
        "state_accuracy": round(sum(r["state_hit"] for r in results) / n, 3),
        "action_accuracy": round(sum(r["action_hit"] for r in results) / n, 3),
        "on_time_rate": round(sum(r["timeliness"] == "on_time" for r in results) / n, 3),
        "total_scenarios": n,
        "per_customer": results,
    }
    if verbose:
        print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--inferred", default="data/inferred_events.json")
    parser.add_argument("--ground_truth", default="data/sample_scenario/ground_truth.json")
    args = parser.parse_args()
    score(args.inferred, args.ground_truth)
