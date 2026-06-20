# AIOps Mini-Platform Spec — Hoang Huy

---

## 1. Platform overview

This AIOps platform monitors a microservices stack of ~5–10 services running on Docker Compose (development/staging) and Kubernetes (production). The platform ingests metrics from Prometheus, logs from a centralized collector, and container lifecycle events from the Docker event stream. It surfaces anomalies, fires alerts, and ranks root-cause candidates for on-call engineers. Primary users are SRE/platform engineers responsible for on-call response. Scope is limited to the services defined in the W3 lab stack (payment-svc, inventory-svc, api-gateway, auth-svc, frontend, and supporting infrastructure).

---

## 2. SLO definition (from W3-D1)

```yaml
# slo_spec.yaml — 3 services × SLI + SLO + error budget

version: 1
services:
  - name: api
    sli:
      name: api_availability
      kind: availability
      formula: "count(2xx,3xx,4xx_not_429) / count(all)"
      promql_good: 'sum(rate(http_requests_total{status!~"5..|429"}[1m]))'
      promql_total: 'sum(rate(http_requests_total[1m]))'
      source: access_log.jsonl
    slo:
      target: 0.997
      window_days: 30
    budget:
      total_events_per_month: 20737800
      allowed_failures_per_month: 207378
      downtime_minutes_equivalent: 199

  - name: db
    sli:
      name: db_query_success
      kind: availability
      formula: "count(success=true) / count(all)"
      promql_good: 'sum(rate(db_queries_total{success="true"}[1m]))'
      promql_total: 'sum(rate(db_queries_total[1m]))'
      source: db_query_log.jsonl
    slo:
      target: 0.995
      window_days: 30
    budget:
      total_events_per_month: 1726390
      allowed_failures_per_month: 8632
      downtime_minutes_equivalent: 96

  - name: frontend
    sli:
      name: frontend_page_load_ok
      kind: availability
      formula: "count(js_error=false AND network_error=false) / count(all)"
      promql_good: 'sum(rate(frontend_page_loads_total{js_error="false",network_error="false"}[1m]))'
      promql_total: 'sum(rate(frontend_page_loads_total[1m]))'
      source: frontend_rum.jsonl
    slo:
      target: 0.990
      window_days: 30
    budget:
      total_events_per_month: 5184000
      allowed_failures_per_month: 51840
      downtime_minutes_equivalent: 576
```

---

## 3. Detection + Correlation + RCA stack (from W1+W2)

Detection layer: The detector uses service-level availability, latency, error-rate, and lifecycle signals to emit alerts with service, metric, severity, and fire timestamp. Signals are evaluated per-service and fire when observed values cross anomaly thresholds derived from baseline windows. Validated performance on W3-D2: precision 1.00, recall 0.90, MTTD p50 1.8s, p95 3.2s, zero false alarms in baseline windows.

Correlation layer: The correlator groups alerts within a short incident window and prefers clusters that share topology edges or a common change window. Incident vectors are 32-dimensional: 18 continuous log/trace dimensions + 14 one-hot service dimensions.

Remediation layer (W2): Three-layer decision engine:


Similarity (Layer 2): Hybrid similarity = 0.7 × cosine(log/trace dims) + 0.3 × Jaccard(service one-hot). Pure cosine over all 32 dimensions was rejected because one-hot service dims are sparse — many dims = 0 on both sides inflates cosine artificially. Jaccard only counts positions where at least one side = 1, avoiding this dilution. Threshold: OOD_THRESHOLD on best_sim to detect out-of-distribution incidents.
Voting (Layer 2): Outcome-weighted voting across top-k neighbours: vote = similarity × outcome_weight. Actions from failed historical outcomes are down-weighted to zero, preventing repetition of previously failed remediation paths.
Decision (Layer 3): Expected Utility scoring per candidate action: EU = p_success × benefit - cost_penalty - blast_penalty. Final action selected only if confidence_adjusted = best_sim × consensus × p_success ≥ CONFIDENCE_FLOOR (0.20). If no action clears the gate, engine falls back to page_oncall.


Known limitation (FINDINGS.md): Confidence gate is currently too conservative — multiplying best_sim × consensus × p_success causes triple attenuation, causing false escalation even when best_sim is high and neighbours are clear (E01: best_sim=0.4967, all neighbours outcome=success, but still escalated). Proposed fix: decouple incident recognition (best_sim gate) from action confidence (consensus × p_success gate) so incidents with clear identity but multiple viable actions are not treated as OOD.
---

## 4. Reliability validation (from W3-D2)

**Chaos experiment scoreboard (W3-D2 pipeline results):**

- Total experiments: 10 | Detected: 9/10 | RCA correct: 8/9
- Precision: 1.00 | Recall: 0.90 | MTTD p50: 1.8s, p95: 3.2s | False alarms in baseline: 0

| # | Experiment | Detected | MTTD | RCA service | RCA correct? |
|---|---|---|---|---|---|
| 1 | payment_latency_500ms | ✅ | 1.8s | payment-svc | ✅ |
| 2 | payment_packet_loss_30pct | ✅ | 1.6s | payment-svc | ✅ |
| 3 | inventory_pod_kill | ✅ | 1.6s | inventory-svc | ✅ |
| 4 | api_gateway_cpu_stress_90pct | ✅ | 2.3s | api-gateway | ✅ |
| 5 | payment_db_memory_fill_95pct | ✅ | 1.1s | payment-db | ✅ |
| 6 | auth_svc_clock_skew_60s | ✅ | 2.9s | auth-svc | ✅ |
| 7 | log_collector_disk_fill_95pct | ✅ | 2.8s | log-collector | ✅ |
| 8 | frontend_api_gateway_full_partition_30s | ✅ | 0.9s | frontend | ❌ (expected api-gateway) |
| 9 | dns_resolver_slow_lookup_2s | ✅ | 3.2s | dns-resolver | ✅ |
| 10 | checkout_svc_http500_inject_20pct | ❌ | — | — | — |

**Top 3 gaps identified:**
1. **RCA topology resolution for network partition (Exp 8):** Pipeline picked `frontend` instead of `api-gateway`. RCA resolves `target` field (partition source) rather than traversing dependency graph to find the downstream critical-path service. Fix requires topology-aware RCA that identifies the service on the downstream side of the partition cut.
2. **Cascade retry / http_error not detected (Exp 10):** `checkout_svc_http500_inject_20pct` — complete false negative. `cascade_retry` fault maps to `http_error` payload but alert fire_ts fell outside query window due to cooldown timing. Pipeline has no dedicated retry-storm detector with a longer window (300s+) to catch this pattern.
3. **Monitoring dependency loop risk (Exp 7 — partial):** log-collector disk fill was detected, but if the pipeline depends on log-collector to scrape metrics, disk fill would cause monitoring blind spot. AIOps stack must have an independent observability path not co-dependent on monitored services (Roblox 2021 pattern).

---

## 5. Operational pattern (from W3-D3)

**Reproduced outage:** AWS S3 us-east-1 2017-02-28 — Operator action without guardrail

**Reproduction summary:** `docker compose stop --remove-orphans` without service-name argument stopped all 3 subsystems (billing, index, placement) simultaneously. S3 metadata (index) and object location (placement) both unavailable → 100% request failure.

**Key learnings:**
1. `--remove-orphans` has no service-scope restriction — any unscoped stop in a compose project affects all services in that project. Runbooks must explicitly document blast radius of each flag.
2. The AIOps pipeline is blind to infrastructure-layer events unless a metrics/event source is explicitly wired in. Operator-action failures are a distinct failure mode category that requires infrastructure event integration, not just application metrics.
3. Blameless framing: the failure was not "operator error" but "maintenance script pipeline allowed unscoped destructive command to reach production without scope validation."

**ADR-001 reference:** Decision to implement two-layer guardrail (script-level prevention + pipeline docker event detection) was driven directly by the two gaps observed in this reproduction.

---

## 6. Cost model (from W3-D3)

**Cost model output for current stack:**

```
=== Scenario: Current lab stack proxy (50 services, Vietnam e-commerce) ===
{
  "monthly_value": 19200.0,
  "monthly_cost": 15000,
  "roi": 1.28,
  "payback_months": 0.78,
  "verdict": "marginal"
}
```

**Interpretation:** At current stack size and incident rate, AIOps investment is marginal (ROI 1.28). Break-even is reached at ~0.78 months. To reach `worth_it` (ROI > 1.5), the platform needs either: (a) incident frequency to increase to ~5/month, or (b) downtime cost to exceed $10k/hour (consistent with larger e-commerce or SaaS platforms).

**Break-even point:** The platform justifies AIOps investment when: `incidents_per_month × avg_duration_hours × downtime_cost_per_hour × 0.4 > $22,500/month` (1.5× the $15k monthly AIOps cost).

---

## 7. Open risks

| Risk | Severity | Mitigation plan |
|---|---|---|
| **Operator-action failures invisible to pipeline** — no docker event stream integration (confirmed W3-D3) | HIGH | ADR-001: add docker event collector; alert on ≥2 services stop in same project within 5s. Target: W4. |
| **Monitoring dependency loop** — if Prometheus goes down, pipeline goes silent with no out-of-band alert (Roblox 2021 pattern) | HIGH | Add external watchdog (separate network path) that pings pipeline health endpoint every 30s. Target: W4. |
| **Network partition RCA misattribution** — count-based tiebreaker biased toward downstream services under retry storm | MEDIUM | Increase topology-distance signal weight from 0.4 → 0.6; re-evaluate after 30 days of production data. Target: W5. |
| **No canary on maintenance scripts** — new scripts added to `scripts/` directory bypass guardrail validation | MEDIUM | CI lint step: reject any script containing `compose stop` or `kubectl delete` without `--service` argument guard. Target: W4. |
| **Cost model assumes constant MTTR reduction** — 40% MTTR reduction is a default estimate, not measured from this stack | LOW | Instrument actual MTTR before/after AIOps deployment; update cost model with empirical reduction rate after 90 days. |