# W3-D1 Data Pack — SLO, Error Budget, Burn-Rate Alerting

## Contents

```
.
├── generate_data.py            # synthetic 7-day log generator
├── docker-compose.yml          # Prometheus + Alertmanager + Grafana
├── configs/
│   ├── prometheus.yml
│   └── alertmanager.yml
├── scripts/
│   ├── compute_baseline.py     # extract baseline SLI from logs
│   ├── validate.py             # eval your alert rules vs ground truth
│   └── prometheus_replay.sh    # optional: push metrics to Prometheus
├── data/                       # generated after running generate_data.py
└── README.md                   # this file
```

## Quick start

```bash
# 1. Install Python deps
uv venv --python 3.12
uv pip install pyyaml

# 2. Generate 7-day synthetic logs (~5 min, 6-8GB output)
uv run python generate_data.py

# 3. Compute baseline SLI
uv run python scripts/compute_baseline.py --data data/ --out baseline.json
cat baseline.json

# 4. Write your slo_spec.yaml and burn_rate_alerts.yaml (see handout §9.3, §9.4)

# 5. Validate
uv run python scripts/validate.py \
  --slo-spec slo_spec.yaml \
  --rules burn_rate_alerts.yaml \
  --truth data/incident_window.csv \
  --data data/ \
  --out validation_report.json

cat validation_report.json
```

## Optional: see metrics in Grafana

```bash
docker compose up -d
# Open http://localhost:3000 — Grafana anonymous Admin
# Open http://localhost:9090 — Prometheus
```

## Schemas

### `data/access_log.jsonl`
```json
{"ts": "2026-06-01T00:00:00+00:00", "method": "GET", "path": "/api/orders",
 "status": 200, "latency_ms": 142}
```

### `data/db_query_log.jsonl`
```json
{"ts": "2026-06-01T00:00:00+00:00", "query": "SELECT ...",
 "duration_ms": 38, "success": true, "rows": 17}
```

### `data/frontend_rum.jsonl`
```json
{"ts": "2026-06-01T00:00:00+00:00", "page": "/products",
 "dom_ready_ms": 1240, "js_error": false, "network_error": false}
```

### `data/incident_window.csv`
```csv
incident_id,layer,severity,start_utc,end_utc,fail_rate_multiplier
1,api,tier1,2026-06-01T03:00:00+00:00,2026-06-01T03:08:00+00:00,100
```

Use this to check your alert rules — `validate.py` does the matching.

## Acceptance

See handout §9.5 + §9.8.

Target:
- `noise_reduction_pct ≥ 70`
- `mttd_delta_s < 60`
- `your_mwmbr.fn = 0`
