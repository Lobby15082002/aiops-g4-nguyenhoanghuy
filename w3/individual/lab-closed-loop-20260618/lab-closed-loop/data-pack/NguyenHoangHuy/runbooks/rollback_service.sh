#!/usr/bin/env bash
# runbooks/rollback_service.sh
# Rollback: restore previous container state by stopping then starting the service.
# Usage: bash rollback_service.sh --service <name> [--dry-run]

set -euo pipefail

SERVICE=""
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service) SERVICE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    *) shift ;;
  esac
done

if [[ -z "$SERVICE" ]]; then
  echo "[ERROR] --service is required" >&2
  exit 1
fi

CONTAINER="ronki-${SERVICE}"

if $DRY_RUN; then
  echo "[DRY-RUN] would execute: rollback ${CONTAINER} (stop + start)"
  exit 0
fi

echo "[rollback_service] Rolling back ${CONTAINER}: stop then start"

docker stop "${CONTAINER}" 2>/dev/null || true
sleep 2
docker start "${CONTAINER}" 2>/dev/null || docker restart "${CONTAINER}" 2>/dev/null || true

echo "[rollback_service] Rollback complete for ${CONTAINER}"
exit 0
