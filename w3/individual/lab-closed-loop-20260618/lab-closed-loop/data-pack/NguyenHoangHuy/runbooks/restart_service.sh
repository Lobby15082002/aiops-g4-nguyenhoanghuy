#!/usr/bin/env bash
# runbooks/restart_service.sh
# Restarts a service by recreating its container via Docker Compose.
#
# IMPORTANT DESIGN NOTE (Docker Desktop on Windows/WSL2):
# `docker restart` only restarts the process inside the SAME container —
# it does NOT reload environment variables from docker-compose.yml/.env,
# and it does NOT reach into the container's network namespace the way a
# host-side `nsenter` would (Docker Desktop runs containers inside its own
# isolated VM; `nsenter -t <pid>` from WSL2/Ubuntu cannot see that PID's
# /proc, so any "fix" that depends on nsenter from the host silently fails).
#
# This script instead:
#   1. Clears any tc/netem network fault using a helper container that
#      shares the target's network namespace (--net=container:<name>),
#      which works regardless of host/VM architecture.
#   2. Recreates the container via `docker compose up -d --force-recreate`
#      so environment variables are reloaded to their compose-file defaults
#      (fixes ENV-based faults, e.g. BASE_LATENCY_MS overrides).
#
# Usage: bash restart_service.sh --service <name> [--dry-run]

set -euo pipefail

SERVICE=""
DRY_RUN=false
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/../../configs/docker-compose.yml"

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
  echo "[DRY-RUN] would execute: clear network fault + docker compose up -d --force-recreate ${SERVICE} (container: ${CONTAINER})"
  exit 0
fi

echo "[restart_service] Clearing any network fault on ${CONTAINER}..."
docker run --rm \
  --pid="container:${CONTAINER}" \
  --net="container:${CONTAINER}" \
  --cap-add=NET_ADMIN \
  nicolaka/netshoot \
  tc qdisc del dev eth0 root 2>/dev/null || true
echo "[restart_service] Network fault cleared (or none was present)."

echo "[restart_service] Recreating container ${CONTAINER} via docker compose (reloads ENV to defaults)..."

if [[ -f "$COMPOSE_FILE" ]]; then
  if docker compose -f "$COMPOSE_FILE" up -d --force-recreate "${SERVICE}"; then
    echo "[restart_service] Successfully recreated ${CONTAINER} via compose."
    exit 0
  else
    echo "[restart_service] compose recreate failed, falling back to docker restart" >&2
    docker restart "${CONTAINER}" && exit 0 || exit 1
  fi
else
  echo "[restart_service] WARNING: compose file not found at ${COMPOSE_FILE}, falling back to docker restart"
  if docker restart "${CONTAINER}"; then
    echo "[restart_service] Successfully restarted ${CONTAINER}"
    exit 0
  else
    echo "[restart_service] FAILED to restart ${CONTAINER}" >&2
    exit 1
  fi
fi
