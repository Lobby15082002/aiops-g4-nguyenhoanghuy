#!/usr/bin/env bash
# runbooks/multi_step_deploy.sh
# Multi-step deploy runbook for transactional rollback testing (Scenario 4).
# Steps: --step-a, --step-b, --step-c (step-c intentionally fails when service is down)
# Rollback steps: --rollback-b, --rollback-a
# Usage: bash multi_step_deploy.sh --service <name> [--step-a|--step-b|--step-c|--rollback-a|--rollback-b] [--dry-run]

set -euo pipefail

SERVICE=""
DRY_RUN=false
STEP=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service)    SERVICE="$2"; shift 2 ;;
    --dry-run)    DRY_RUN=true; shift ;;
    --step-a)     STEP="step-a"; shift ;;
    --step-b)     STEP="step-b"; shift ;;
    --step-c)     STEP="step-c"; shift ;;
    --rollback-a) STEP="rollback-a"; shift ;;
    --rollback-b) STEP="rollback-b"; shift ;;
    *) shift ;;
  esac
done

if [[ -z "$SERVICE" ]]; then
  echo "[ERROR] --service is required" >&2
  exit 1
fi

CONTAINER="ronki-${SERVICE}"

if $DRY_RUN; then
  echo "[DRY-RUN] would execute: multi_step_deploy ${STEP} on ${CONTAINER}"
  exit 0
fi

case "$STEP" in
  step-a)
    echo "[multi_step_deploy] Step A: preparing config for ${CONTAINER}"
    # Simulate: update environment variables / config
    docker exec "${CONTAINER}" sh -c "echo 'step-a: config updated'" 2>/dev/null || \
      echo "[multi_step_deploy] Step A simulated (exec not available)"
    echo "[multi_step_deploy] Step A complete"
    exit 0
    ;;

  step-b)
    echo "[multi_step_deploy] Step B: hot-reload app on ${CONTAINER}"
    docker exec "${CONTAINER}" sh -c "echo 'step-b: reload triggered'" 2>/dev/null || \
      echo "[multi_step_deploy] Step B simulated"
    echo "[multi_step_deploy] Step B complete"
    exit 0
    ;;

  step-c)
    echo "[multi_step_deploy] Step C: health-check final deploy on ${CONTAINER}"
    # Step C fails if container is not running (tests transactional rollback)
    if ! docker inspect --format='{{.State.Running}}' "${CONTAINER}" 2>/dev/null | grep -q "true"; then
      echo "[multi_step_deploy] Step C FAILED: ${CONTAINER} is not running" >&2
      exit 1
    fi
    docker exec "${CONTAINER}" sh -c "echo 'step-c: smoke test passed'" 2>/dev/null
    echo "[multi_step_deploy] Step C complete"
    exit 0
    ;;

  rollback-b)
    echo "[multi_step_deploy] Rollback B: reverting hot-reload on ${CONTAINER}"
    docker exec "${CONTAINER}" sh -c "echo 'rollback-b: reload reverted'" 2>/dev/null || \
      echo "[multi_step_deploy] Rollback B simulated"
    echo "[multi_step_deploy] Rollback B complete"
    exit 0
    ;;

  rollback-a)
    echo "[multi_step_deploy] Rollback A: reverting config on ${CONTAINER}"
    docker exec "${CONTAINER}" sh -c "echo 'rollback-a: config reverted'" 2>/dev/null || \
      echo "[multi_step_deploy] Rollback A simulated"
    echo "[multi_step_deploy] Rollback A complete"
    exit 0
    ;;

  "")
    echo "[ERROR] No step specified. Use --step-a, --step-b, --step-c, --rollback-a, --rollback-b" >&2
    exit 1
    ;;

  *)
    echo "[ERROR] Unknown step: ${STEP}" >&2
    exit 1
    ;;
esac
