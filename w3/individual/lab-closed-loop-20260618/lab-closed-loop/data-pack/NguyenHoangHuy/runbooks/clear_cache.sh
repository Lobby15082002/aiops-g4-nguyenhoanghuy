#!/usr/bin/env bash
# runbooks/clear_cache.sh
# Clears application cache by sending a signal / exec command to the container.
# Usage: bash clear_cache.sh --service <name> [--dry-run]

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
  echo "[DRY-RUN] would execute: docker exec ${CONTAINER} /app/scripts/clear_cache.sh"
  exit 0
fi

echo "[clear_cache] Clearing cache on ${CONTAINER}"

# Try exec first; fall back to restart if exec is not available
if docker exec "${CONTAINER}" sh -c "[ -f /app/scripts/clear_cache.sh ] && sh /app/scripts/clear_cache.sh || kill -USR1 1" 2>/dev/null; then
  echo "[clear_cache] Cache cleared on ${CONTAINER}"
  exit 0
else
  echo "[clear_cache] exec failed; restarting ${CONTAINER} to clear cache"
  docker restart "${CONTAINER}"
  echo "[clear_cache] Restarted ${CONTAINER}"
  exit 0
fi
