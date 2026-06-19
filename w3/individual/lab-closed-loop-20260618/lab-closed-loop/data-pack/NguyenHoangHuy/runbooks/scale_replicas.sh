#!/usr/bin/env bash
# runbooks/scale_replicas.sh
# Scales replicas for a service (Docker Compose scale).
# Usage: bash scale_replicas.sh --service <name> [--replicas <n>] [--dry-run]

set -euo pipefail

SERVICE=""
REPLICAS=2
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service)  SERVICE="$2";   shift 2 ;;
    --replicas) REPLICAS="$2";  shift 2 ;;
    --dry-run)  DRY_RUN=true;   shift ;;
    *) shift ;;
  esac
done

if [[ -z "$SERVICE" ]]; then
  echo "[ERROR] --service is required" >&2
  exit 1
fi

CONTAINER="ronki-${SERVICE}"

if $DRY_RUN; then
  echo "[DRY-RUN] would execute: docker compose scale ${SERVICE}=${REPLICAS}"
  exit 0
fi

echo "[scale_replicas] Scaling ${SERVICE} to ${REPLICAS} replicas"

# For docker compose v2
COMPOSE_FILE="$(pwd)/../../configs/docker-compose.yml"
if [[ -f "$COMPOSE_FILE" ]]; then
  docker compose -f "$COMPOSE_FILE" up -d --scale "${SERVICE}=${REPLICAS}" --no-recreate
  echo "[scale_replicas] Scaled ${SERVICE} to ${REPLICAS}"
  exit 0
else
  # Fallback: try docker restart
  docker restart "${CONTAINER}"
  echo "[scale_replicas] Fallback: restarted ${CONTAINER}"
  exit 0
fi
