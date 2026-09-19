"""Generates a small synthetic scenario under data/sample_scenario/ so the
pipeline can be run and eval'd end-to-end without the real mega.nz dataset
(useful for local dev / CI; swap in the real scenario dirs for actual
grading runs). Produces history_seed.jsonl, live_stream.jsonl, entities.json,
replay_config.json, and a ground_truth.json for eval.py to score against.
"""
import json
import os
import random
from datetime import datetime, timedelta, timezone

random.seed(7)
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "sample_scenario")
os.makedirs(OUT, exist_ok=True)

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
_eid = [0]


def eid():
    _eid[0] += 1
    return f"EVT_{_eid[0]:06d}"


def ev(t, cid, acc, src, etype, payload):
    return {
        "event_id": eid(), "event_time": t.isoformat().replace("+00:00", "Z"),
        "ingestion_time": (t + timedelta(seconds=3)).isoformat().replace("+00:00", "Z"),
        "customer_id": cid, "account_id": acc, "source_system": src,
        "event_type": etype, "schema_version": "1.0", "payload": payload,
    }


history, live, ground_truth = [], [], []

# --- Customer 1: new_child_life_event ------------------------------------
cid = "CUST_00042"
t = BASE
for i in range(20):
    history.append(ev(t, cid, "ACC_CHK_001", "web_app_events", "login",
                       {"feature_or_page": "home", "device_type": "mobile", "session_length_sec": 120}))
    history.append(ev(t + timedelta(hours=1), cid, "ACC_CHK_001", "card_payments", "purchase",
                       {"merchant_name": "Grocery Mart", "mcc_category": "grocery", "amount": round(random.uniform(20, 80), 2),
                        "currency": "USD", "is_international": False, "card_present": True}))
    t += timedelta(days=3)

live_start = t
live.append(ev(t, cid, "ACC_CHK_001", "card_payments", "purchase",
                {"merchant_name": "City Pharmacy", "mcc_category": "pharmacy", "amount": 64.20,
                 "currency": "USD", "is_international": False, "card_present": True}))
t += timedelta(days=1)
live.append(ev(t, cid, "ACC_CHK_001", "web_app_events", "search_query",
                {"search_text": "best daycare near me", "device_type": "mobile"}))
t += timedelta(days=2)
live.append(ev(t, cid, "ACC_CHK_001", "card_payments", "purchase",
                {"merchant_name": "Baby World", "mcc_category": "pharmacy", "amount": 210.00,
                 "currency": "USD", "is_international": False, "card_present": True}))
t += timedelta(days=1)
live.append(ev(t, cid, None, "loan_kyc", "dependents_change",
                {"event_subtype": "dependents_change", "old_value": 0, "new_value": 1}))
ground_truth.append({"customer_id": cid, "expected_state": "new_child_life_event",
                      "expected_action": "PERSONALIZED_OFFER", "by": t.isoformat()})

# --- Customer 2: churn_risk -------------------------------------------------
cid = "CUST_00099"
t = BASE
for i in range(20):
    history.append(ev(t, cid, "ACC_CHK_002", "web_app_events", "login",
                       {"feature_or_page": "home", "device_type": "web", "session_length_sec": 300}))
    t += timedelta(days=2)

for i in range(3):  # sparse recent logins -> negative trend
    live.append(ev(t, cid, "ACC_CHK_002", "web_app_events", "login",
                    {"feature_or_page": "home", "device_type": "web", "session_length_sec": 30}))
    t += timedelta(days=10)
live.append(ev(t, cid, "ACC_CHK_002", "support_logs", "ticket_created",
                {"channel": "chat", "category": "billing",
                 "raw_text": "This is terrible, I've been waiting for a refund for weeks and nobody has helped. Very frustrated and disappointed.",
                 "resolution_status": "open"}))
t += timedelta(days=1)
live.append(ev(t, cid, "ACC_CHK_002", "web_app_events", "feature_used",
                {"feature_or_page": "account_closure", "device_type": "web"}))
ground_truth.append({"customer_id": cid, "expected_state": "churn_risk",
                      "expected_action": "PROACTIVE_RETENTION_OUTREACH", "by": t.isoformat()})

# --- Customer 3: potential_fraud_or_takeover (guardrail hard-stop) --------
cid = "CUST_00150"
t = BASE
for i in range(10):
    history.append(ev(t, cid, "ACC_CHK_003", "card_payments", "purchase",
                       {"merchant_name": "Coffee Shop", "mcc_category": "restaurant", "amount": round(random.uniform(5, 15), 2),
                        "currency": "USD", "is_international": False, "card_present": True}))
    t += timedelta(days=4)
live.append(ev(t, cid, "ACC_CHK_003", "support_logs", "ticket_created",
                {"channel": "phone", "category": "fraud",
                 "raw_text": "I noticed an unauthorized transaction on my account for $2,400 that I did not authorize. I think my account was hacked.",
                 "resolution_status": "open"}))
ground_truth.append({"customer_id": cid, "expected_state": "potential_fraud_or_takeover",
                      "expected_action": "COMPLIANCE_FRAUD_HOLD", "by": t.isoformat()})

history.sort(key=lambda e: e["event_time"])
live.sort(key=lambda e: e["event_time"])

with open(os.path.join(OUT, "history_seed.jsonl"), "w") as f:
    for e in history:
        f.write(json.dumps(e) + "\n")
with open(os.path.join(OUT, "live_stream.jsonl"), "w") as f:
    for e in live:
        f.write(json.dumps(e) + "\n")
with open(os.path.join(OUT, "entities.json"), "w") as f:
    json.dump({"customers": [
        {"customer_id": "CUST_00042", "name": "Synthetic Customer A", "accounts": ["ACC_CHK_001"]},
        {"customer_id": "CUST_00099", "name": "Synthetic Customer B", "accounts": ["ACC_CHK_002"]},
        {"customer_id": "CUST_00150", "name": "Synthetic Customer C", "accounts": ["ACC_CHK_003"]},
    ]}, f, indent=2)
with open(os.path.join(OUT, "replay_config.json"), "w") as f:
    json.dump({"pacing": "instant", "note": "synthetic scenario for local dev/testing"}, f, indent=2)
with open(os.path.join(OUT, "instructions.md"), "w") as f:
    f.write("# Synthetic sample scenario\nGenerated for local end-to-end testing. "
            "Three storylines: new-child life event, churn risk, and a fraud hard-stop.\n")
with open(os.path.join(OUT, "ground_truth.json"), "w") as f:
    json.dump(ground_truth, f, indent=2)

print(f"Wrote {len(history)} history events, {len(live)} live events -> {OUT}")
