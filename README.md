# Agentic Customer 360  Proactive Intervention Desk



## Fresh clone on a new machine
```bash
git clone <your-repo-url>
cd customer360
python3 -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt
python scripts/generate_sample_scenario.py           # only needed to regenerate the demo data
python main.py --scenario_dir data/sample_scenario --hitl_mode auto
python eval.py
```
Requires Python 3.9+ (uses only the standard library plus
`opentelemetry-api`/`opentelemetry-sdk`, both pinned in
`requirements.txt`). No external services, API keys, or network access
are needed to run it — everything (guardrails, RAG, memory, tracing) is
local/offline by design (see `research_log.md` R1/R5 for why).

Generated run artifacts (`data/inferred_events.json`, `data/traces.jsonl`,
`data/metrics.json`, `data/hitl_audit_log.jsonl`, `data/episodic/`,
`data/policy.json`) are gitignored  each machine/run regenerates its own.
`data/sample_scenario/` (the synthetic test fixtures) IS committed since
it's static input data, not output.

## Quickstart
```bash
pip install -r requirements.txt

# generate a small synthetic scenario for local testing (3 storylines:
# new-child life event, churn risk, fraud hard-stop)
python scripts/generate_sample_scenario.py

# run the pipeline (batch/auto HITL mode)
python main.py --scenario_dir data/sample_scenario --hitl_mode auto

# run with a REAL interactive human-approval gate
python main.py --scenario_dir data/sample_scenario --hitl_mode interactive

# score against ground truth
python eval.py --inferred data/inferred_events.json \
                --ground_truth data/sample_scenario/ground_truth.json
```

For the real competition scenarios, drop each scenario's
`entities.json` / `history_seed.jsonl` / `live_stream.jsonl` /
`replay_config.json` into a directory and pass it as `--scenario_dir`.

## Layout
```
agents/         one file per agent (usage, support, transaction, kyc,
                life_event, synthesis, offer, retention, critique,
                guardrail) + base_agent.py + hitl.py
memory/         state_board.py (working memory), episodic_memory.py
                (episodic + semantic memory), vector_rag.py (live TF-IDF)
guardrails/     keyword_scanner.py (deterministic hard-stop scan),
                pii.py (masking applied at the data-access boundary)
observability/  tracing.py (OpenTelemetry spans + metrics)
data/           sample_scenario/, and run outputs: inferred_events.json,
                traces.jsonl, metrics.json, hitl_audit_log.jsonl,
                episodic/<customer_id>.json, policy.json
main.py         stream ingestion + orchestration
eval.py         scoring harness vs. ground_truth.json
```

## Outputs of a run
- `data/inferred_events.json` the required checkpoint schema
  (`as_of_time, customer_id, inferred_state, confidence_band, action,
  action_subtype, hitl_status, notes, citations`).
- `data/traces.jsonl`  full OpenTelemetry trace log (one span per line).
- `data/metrics.json`  per-stage latency, per-agent confidence, HITL/
  action/guardrail counts.
- `data/hitl_audit_log.jsonl` every HITL decision with full context.
- `data/episodic/<customer_id>.json` persisted per-customer memory.
