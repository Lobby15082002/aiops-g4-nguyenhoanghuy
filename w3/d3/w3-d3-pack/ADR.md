# ADR-001: Operator Action Guardrail Strategy for AIOps Platform Maintenance Scripts

## Status
Accepted

## Context

During W3-D3 outage reproduction (AWS S3 2017 — operator typo), the AIOps pipeline failed to detect a complete 3-service outage caused by an unscoped `docker compose stop --remove-orphans` command. Two critical gaps were identified:

1. **Detection gap:** Pipeline has no integration with infrastructure-layer events (container lifecycle). Operator-action failures that don't generate application-layer metrics are invisible to the current stack.
2. **Prevention gap:** Maintenance scripts have no scope-validation or blast-radius check before executing destructive commands.

The platform needs a decision on **where to enforce guardrails** — at the script level (prevention), at the alerting level (detection), or both — and what the acceptable trade-off is between operational speed and safety.

## Decision

Implement a **two-layer guardrail**:

1. **Script-level prevention:** All maintenance scripts that invoke destructive compose/kubectl commands must require an explicit `--service` or `--scope` argument. Scripts must print a dry-run summary and prompt for confirmation before execution. Unscoped destructive commands are rejected at the script entry point.

2. **Pipeline-level detection:** Add a docker event stream collector to the AIOps pipeline. Fire a `multi-service-stop` alert when ≥2 services in the same compose project stop within a 5-second window. This feeds into the existing RCA pipeline as a new alert source.

## Alternatives considered

- **Prevention only (script validation, no pipeline change)**
  - Pro: simple, low operational overhead, no new infra required
  - Con: does not close detection gap — if a guardrail is bypassed or a new script is added without validation, the pipeline remains blind. Rejected as sole approach because defense-in-depth is required for P0 failure modes.

- **Detection only (pipeline docker event integration, no script change)**
  - Pro: catches failure regardless of how it was triggered; improves pipeline coverage broadly
  - Con: detection fires *after* the outage has started; does not prevent blast radius. Rejected as sole approach because a 4-hour outage (original S3 incident) is not acceptable when prevention is straightforward.

- **No change — accept operator error as human factor**
  - Pro: zero engineering cost
  - Con: same failure mode will recur; postmortem action items become fiction. Rejected — blameless culture requires systemic fix, not individual accountability.

## Consequences

**Positive:**
- Script-level guardrail eliminates the specific failure mode reproduced in W3-D3 (unscoped destructive command)
- Pipeline docker event integration closes Detection Gap 2 identified in postmortem §Detection — operator-action failures become visible to AIOps stack
- Two-layer approach means either layer catching the issue independently; single point of failure is eliminated

**Negative (trade-offs accepted):**
- Script validation adds ~5–10 seconds of confirmation latency to every maintenance operation — acceptable for destructive commands, minor friction for high-frequency ops
- Docker event collector adds a new data source to maintain; pipeline topology graph must be kept current with compose project names

**Risks:**
- Risk: engineers write new scripts that bypass the validation wrapper. Mitigation: CI lint step checks all scripts in `scripts/` directory for unguarded compose/kubectl destructive calls.
- Risk: docker event stream adds noise to RCA if non-critical containers stop frequently. Mitigation: alert only on ≥2 services in same project within 5s; tune threshold after 30 days of data.
