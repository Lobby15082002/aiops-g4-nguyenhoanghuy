#!/usr/bin/env bash
# inject_fault_patched.sh — Docker-Desktop-compatible version of inject_fault.sh
#
# WHY THIS EXISTS:
# The original inject_fault.sh uses `nsenter -t <PID> -n -- tc ...` to reach
# into a container's network namespace from the host. This works on native
# Linux Docker Engine, but NOT on Docker Desktop (Windows/Mac), because
# Docker Desktop runs containers inside an isolated VM whose /proc is not
# visible to the WSL2/host `nsenter`. Confirmed via:
#   nsenter -t <PID> -n -- tc qdisc show dev eth0
#   → "cannot open /proc/<PID>/ns/net: No such file or directory"
#
# FIX: use a short-lived helper container that shares the target's network
# namespace via `--net=container:<name>`, which works regardless of host
# architecture because it goes through the Docker daemon API instead of
# touching /proc directly.
#
# Usage identical to original inject_fault.sh:
#   bash inject_fault_patched.sh latency <container> <delay>
#   bash inject_fault_patched.sh clear-latency <container>
#   bash inject_fault_patched.sh kill|pause|resume|recover <container>

set -euo pipefail

FAULT="${1:-}"
CONTAINER="${2:-}"
PARAM="${3:-}"

if [[ -z "$FAULT" || -z "$CONTAINER" ]]; then
  echo "Usage: $0 <fault_type> <container_name> [param]"
  echo "       fault_type: latency | kill | pause | resume | recover | clear-latency | --concurrent"
  exit 1
fi

if ! docker inspect "$CONTAINER" > /dev/null 2>&1; then
  CONTAINER="ronki-${CONTAINER}"
fi

if ! docker inspect "$CONTAINER" > /dev/null 2>&1; then
  echo "[inject_fault] ERROR: container '$CONTAINER' not found."
  docker ps --format '  {{.Names}}'
  exit 1
fi

netshoot_tc() {
  # $1 = tc subcommand args
  docker run --rm \
    --net="container:${CONTAINER}" \
    --cap-add=NET_ADMIN \
    nicolaka/netshoot \
    tc $1
}

case "$FAULT" in
  latency)
    DELAY="${PARAM:-200ms}"
    DELAY_MS="${DELAY//ms/}"
    echo "[inject_fault] Adding ${DELAY} network latency to $CONTAINER via netshoot helper..."
    netshoot_tc "qdisc add dev eth0 root netem delay ${DELAY_MS}ms" 2>/dev/null \
      || netshoot_tc "qdisc change dev eth0 root netem delay ${DELAY_MS}ms"
    echo "[inject_fault] Latency ${DELAY} applied to $CONTAINER."
    ;;

  clear-latency)
    echo "[inject_fault] Removing tc netem rules from $CONTAINER via netshoot helper..."
    netshoot_tc "qdisc del dev eth0 root" 2>/dev/null || true
    echo "[inject_fault] Latency rules cleared."
    ;;

  kill)
    echo "[inject_fault] Stopping container $CONTAINER (simulate crash)..."
    docker stop "$CONTAINER"
    echo "[inject_fault] $CONTAINER stopped."
    ;;

  pause)
    docker pause "$CONTAINER"
    echo "[inject_fault] $CONTAINER paused."
    ;;

  resume)
    docker unpause "$CONTAINER"
    echo "[inject_fault] $CONTAINER resumed."
    ;;

  recover)
    docker start "$CONTAINER"
    echo "[inject_fault] $CONTAINER started."
    ;;

  --concurrent)
    SVC1="${CONTAINER}"
    SVC2="${PARAM}"
    if [[ -z "$SVC1" || -z "$SVC2" ]]; then
      echo "[inject_fault] --concurrent requires exactly 2 container names"
      exit 1
    fi
    echo "[inject_fault] Injecting latency fault concurrently on $SVC1 and $SVC2..."
    (bash "$0" latency "$SVC1" 500ms) &
    PID1=$!
    (bash "$0" latency "$SVC2" 500ms) &
    PID2=$!
    wait "$PID1"
    wait "$PID2"
    echo "[inject_fault] Concurrent fault injection complete."
    ;;

  *)
    echo "[inject_fault] Unknown fault type: $FAULT"
    exit 1
    ;;
esac