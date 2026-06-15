#!/usr/bin/env bash
# Replay generated 7-day logs into local Prometheus as if they were live scrapes.
#
# Strategy: convert access_log.jsonl into OpenMetrics text format scraped at
# 1-minute resolution, push via Prometheus remote_write or use a sidecar.
#
# Minimum implementation: emit per-minute counters to a textfile collector and
# rely on node-exporter textfile module. For this assignment, a simpler path:
# use validate.py directly — it reads logs and simulates alert firing without
# needing actual Prometheus to ingest history.
#
# This script is kept for students who WANT to see metrics in Grafana.
set -euo pipefail

DATA_DIR="${1:-data}"
OUT_DIR="${2:-prometheus_textfile}"
mkdir -p "$OUT_DIR"

echo "Aggregating logs into per-minute counters..."
uv run python - <<PY
import json
from pathlib import Path
from collections import defaultdict
from datetime import datetime

buckets = defaultdict(lambda: {"total": 0, "fail": 0, "latency_sum": 0})
for line in Path("${DATA_DIR}/access_log.jsonl").open():
    ev = json.loads(line)
    ts = datetime.fromisoformat(ev["ts"])
    m = ts.replace(second=0, microsecond=0).isoformat()
    b = buckets[m]
    b["total"] += 1
    if ev["status"] >= 500 or ev["status"] == 429:
        b["fail"] += 1
    b["latency_sum"] += ev["latency_ms"]

out = Path("${OUT_DIR}/access_metrics.prom")
with out.open("w") as f:
    f.write("# HELP http_requests_total Total requests\n")
    f.write("# TYPE http_requests_total counter\n")
    cumulative_total = 0
    cumulative_fail = 0
    for m in sorted(buckets):
        b = buckets[m]
        cumulative_total += b["total"]
        cumulative_fail += b["fail"]
        # NOTE: Prometheus expects monotonically increasing counters
        # For replay, we emit final cumulative values
    f.write(f'http_requests_total{{service="api",status="all"}} {cumulative_total}\n')
    f.write(f'http_requests_total{{service="api",status="fail"}} {cumulative_fail}\n')
print(f"Wrote {out}")
PY

echo ""
echo "DONE. For full replay into Prometheus storage, use the 'promtool tsdb create-blocks-from openmetrics'"
echo "command — see https://prometheus.io/docs/prometheus/latest/storage/#backfilling-from-openmetrics-format"
echo ""
echo "For assignment validation, you DON'T need this — use validate.py directly:"
echo "  uv run python scripts/validate.py --rules burn_rate_alerts.yaml --truth data/incident_window.csv --slo-spec slo_spec.yaml"
