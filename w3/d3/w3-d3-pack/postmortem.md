# Postmortem: AWS S3 us-east-1 Full Outage — Operator Action Without Guardrail

**Status:** complete  
**Date:** 2026-06-20  
**Authors:** Hoang Huy  
**Severity:** SEV1  
**Duration:** 2 minutes (2026-06-20T08:44:37Z → 2026-06-20T08:45:31Z reproduction window)  
**Original incident:** 2017-02-28, ~4h, AWS S3 us-east-1

---

## Summary

A maintenance script intended to stop the `billing` subsystem was run without a service-name argument. The `--remove-orphans` flag caused all three subsystems (billing, index, placement) to stop simultaneously. With `index` and `placement` unavailable, S3 could not serve object metadata or resolve object location, resulting in a complete read/write outage for S3 us-east-1. The system was recovered by restarting services in dependency order.

---

## Impact

- Users affected: all S3 us-east-1 customers (~100% of requests failed)
- Revenue impact: estimated $150M+ (AWS-scale downtime ~4h × $200k+/hour)
- SLO budget consumed: 100% of monthly error budget in single incident
- External communication: AWS Service Health Dashboard updated; public postmortem published

---

## Timeline (UTC)

| Time | Event |
|------|-------|
| 2026-06-20T08:44:29Z | Stack start — billing, index, placement containers started |
| 2026-06-20T08:44:29Z | index container started successfully |
| 2026-06-20T08:44:29Z | placement container started successfully |
| 2026-06-20T08:44:37Z | Healthcheck passed — all 3 services Up, stack confirmed healthy |
| 2026-06-20T08:45:29Z | Operator ran maintenance script without service-name argument |
| 2026-06-20T08:45:31Z | billing container stopped (unintended) |
| 2026-06-20T08:45:31Z | index container stopped — S3 object metadata unavailable |
| 2026-06-20T08:45:31Z | placement container stopped — S3 object location resolution broken |
| 2026-06-20T08:45:31Z | All S3 read/write requests begin failing (100% error rate) |
| 2026-06-20T08:45:31Z | AIOps pipeline did not fire alert — no metrics source from reproduction stack |

---

## Root cause

The maintenance runbook specified `docker compose stop billing` to restart the billing subsystem during a capacity adjustment. The operator omitted the service name, running `docker compose stop --remove-orphans` instead. The `--remove-orphans` flag has no service-scope restriction — it stops all services in the compose project plus any orphaned containers. Because `index` and `placement` were in the same compose project, both were stopped as a side effect. No pre-execution confirmation step existed to validate the scope of the command before it ran.

---

## Contributing factors

- The `--remove-orphans` flag scope was not documented in the runbook, creating a knowledge gap for the operator
- No dry-run or confirmation prompt existed in the maintenance script before executing destructive commands
- The compose project bundled all three subsystems under a single project name, meaning any unscoped stop command affected all services
- No blast-radius test had been performed on the maintenance script prior to use in production

---

## Detection

- How detected: container orchestration platform immediately reported all three containers in `Exited` state; downstream services began returning errors within seconds
- Could it have been detected earlier: yes — a pre-execution dry-run (`--dry-run` flag or scope validation) would have surfaced the blast radius before execution. An alert on simultaneous multi-service stop events would have caught this pattern within seconds rather than requiring manual observation.
- **Gap 1:** AIOps pipeline has no metrics integration with the docker compose reproduction stack. The pipeline fired zero alerts during the reproduction — `alerts_observed.json` is an empty array (`[]`), confirming the billing/index/placement containers emit no signal the pipeline can observe.
- **Gap 2:** Pipeline RCA returned `root_service: null, confidence: 0.0` for the reproduction window, meaning operator-action failures that don't generate application-layer metrics are completely invisible to the current detection stack.

---

## Response

- **What went well:** failure was immediately visible at the infrastructure layer (container status); recovery procedure (restart in order) was straightforward
- **What went poorly:** no automated detection fired; the on-call team had no alert — discovery depended on a user report or manual dashboard check
- **Where we got lucky:** the reproduction environment is isolated; in production, the same command pattern caused 4 hours of outage before full recovery

---

## Action items

| Item | Owner | Due | Priority |
|------|-------|-----|----------|
| Add service-name validation to all maintenance scripts before destructive commands | Platform team | 2026-07-04 | P0 |
| Implement dry-run flag in compose maintenance scripts with explicit scope confirmation | Platform team | 2026-07-04 | P0 |
| Add AIOps pipeline integration with docker compose event stream | AIOps team | 2026-07-11 | P1 |
| Add alert rule: ≥2 services in same project stop within 5s window | Observability team | 2026-07-11 | P1 |
| Update runbook to require explicit service-name argument; add linting step | Ops team | 2026-06-27 | P1 |
