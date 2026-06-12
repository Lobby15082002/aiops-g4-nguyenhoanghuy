# Lab — Evidence-Driven Remediation Engine — Data Pack

This pack contains everything you need to run the lab described in the handout.

## Contents

```
data-pack/

├── eval/
│   ├── E01.json ... E08.json          (8 evaluation incidents)
│   └── expected.json                  (ground-truth accepted actions)
├── incidents_history.json             (~29 past incidents)
├── topology.json                      (canonical service topology)
├── actions.yaml                       (remediation action catalog)
├── grade.py                           (auto-grader — run after you produce audit.jsonl)
├── engine_skeleton.py                 (optional starting skeleton — feel free to ignore)
├── optional-helpers.py                (two pure-mechanical schema parsers — see HANDOUT §2.6)
├── features.py                        (Layer 1 — feature extraction)
├── retrieval.py                       (Layer 2 — precedent retrieval + outcome-weighted voting)
├── decision.py                        (Layer 3 — action selection / EU + blast-radius gate)
├── engine.py                          (entry point — CLI wiring all 3 layers)
├── audit.jsonl                        (engine's decisions for E01-E08)
├── FINDINGS.md                        (reflection questions, see handout §5)
└── README.md                          (this file)
```

## Quick start

```bash
unzip lab-w2-evidence-driven-remediation-*.zip
cd data-pack

# Setup (Anaconda / standard Python — pyyaml is the only extra dependency)
conda activate base   # or your environment of choice
pip install pyyaml

# Run on a single eval incident:
python engine.py decide --incident eval/E01.json \
                         --history incidents_history.json \
                         --actions actions.yaml

# Run all 8 eval incidents at once (overwrites audit.jsonl):
python engine.py batch --eval-dir eval \
                        --history incidents_history.json \
                        --actions actions.yaml

# Auto-grade your audit.jsonl:
python grade.py --audit audit.jsonl --expected eval/expected.json
```

Each `decide` run prints the decision JSON to stdout and appends one line to `audit.jsonl`. `batch` clears `audit.jsonl` first and writes one entry per incident (E01-E08).

## Reading the schemas

- `eval/E*.json` — see handout §2.1.
- `incidents_history.json` — see handout §2.2.
- `actions.yaml` — see handout §2.3.
- `eval/expected.json` — `accepted_actions` is a list; engine recommending any one of them gets credit. `must_not_action` is a hard veto.
- `topology.json` — same structure as `eval/E*.json.topology` (nodes + edges).

## Pipeline overview

1. **Layer 1 (`features.py`)** — converts each incident (raw logs/traces/metrics/topology, or historical signatures) into a 32-dimensional feature vector (10 log-keyword clusters + 2 log-level counts + 4 trace features + 1 metric feature + 1 topology-depth feature + 14 service one-hot dims).
2. **Layer 2 (`retrieval.py`)** — compares the new incident's vector against all ~29 historical vectors using hybrid similarity (70% cosine on log/trace dims + 30% Jaccard on service dims), takes the top-5 neighbours, and runs outcome-weighted voting to produce candidate actions with confidence estimates.
3. **Layer 3 (`decision.py`)** — computes Expected Utility for each candidate action (P_success × benefit − cost penalty − blast-radius penalty), applies confidence/blast-radius gates, and selects the final action (or escalates to `page_oncall`).

## Submission

See handout §7.