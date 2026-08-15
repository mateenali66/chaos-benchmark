#!/usr/bin/env bash
set -euo pipefail

################################################################################
# Watchdog Launcher
# Starts infra-watchdog.py + experiment-watchdog.py in the background (nohup)
# for one cluster, logging under data/watchdogs/<env>/.
#
# Usage: ./scripts/watchdogs/watch-all.sh <bench-a|bench-b|ml> [--stall-minutes N]
#
# NOTE: the experiment watchdog can only check runner-process liveness if you
# write a pidfile when you launch run-all-experiments.sh, e.g.:
#   nohup ./scripts/run-all-experiments.sh > data/watchdogs/bench-a/run-all.log 2>&1 &
#   echo $! > data/watchdogs/bench-a/run-all.pid
# Do that BEFORE running this script so the pidfile already exists.
################################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

ENV_NAME="${1:-}"
case "$ENV_NAME" in
  bench-a) CLUSTER_NAME="is-chaos-bench-a" ;;
  bench-b) CLUSTER_NAME="is-chaos-bench-b" ;;
  ml)      CLUSTER_NAME="is-chaos-ml" ;;
  *)
    echo "Usage: $0 <bench-a|bench-b|ml> [--stall-minutes N]" >&2
    exit 1
    ;;
esac
shift || true

STALL_MINUTES="25"
if [[ "${1:-}" == "--stall-minutes" ]]; then
  STALL_MINUTES="${2:?--stall-minutes requires a value}"
fi

KUBE_CONTEXT="is-chaos-${ENV_NAME}"
WATCHDOG_DIR="${CHAOS_DATA_DIR:-${PROJECT_DIR}/data}/watchdogs"
PROGRESS_LOG="${CHAOS_DATA_DIR:-${PROJECT_DIR}/data}/progress.log"

mkdir -p "$WATCHDOG_DIR"

INFRA_STATUS="${WATCHDOG_DIR}/infra-status.jsonl"
INFRA_OUT="${WATCHDOG_DIR}/infra-watchdog.out"
EXPERIMENT_STATUS="${WATCHDOG_DIR}/experiment-status.jsonl"
EXPERIMENT_OUT="${WATCHDOG_DIR}/experiment-watchdog.out"
PIDFILE="${WATCHDOG_DIR}/run-all.pid"

echo "=== Starting watchdogs for ${CLUSTER_NAME} (context: ${KUBE_CONTEXT}) ==="
echo "Logs: ${WATCHDOG_DIR}"
echo ""

if [[ ! -f "$PIDFILE" ]]; then
  echo "NOTE: ${PIDFILE} does not exist yet. The experiment watchdog will report"
  echo "      pid_status=pidfile-not-found until you write it when you launch"
  echo "      run-all-experiments.sh (see header comment of this script)."
  echo ""
fi

nohup python3 -u "${SCRIPT_DIR}/infra-watchdog.py" "$KUBE_CONTEXT" \
  --status-file "$INFRA_STATUS" \
  >> "$INFRA_OUT" 2>&1 &
INFRA_PID=$!
echo "infra-watchdog.py       PID ${INFRA_PID}   status: ${INFRA_STATUS}"

nohup python3 -u "${SCRIPT_DIR}/experiment-watchdog.py" "$PROGRESS_LOG" \
  --status-file "$EXPERIMENT_STATUS" \
  --pidfile "$PIDFILE" \
  --stall-minutes "$STALL_MINUTES" \
  >> "$EXPERIMENT_OUT" 2>&1 &
EXPERIMENT_PID=$!
echo "experiment-watchdog.py  PID ${EXPERIMENT_PID}   status: ${EXPERIMENT_STATUS}"

echo "${INFRA_PID}" > "${WATCHDOG_DIR}/infra-watchdog.pid"
echo "${EXPERIMENT_PID}" > "${WATCHDOG_DIR}/experiment-watchdog.pid"

echo ""
echo "=== Watchdogs running in background ==="
echo "Tail output:"
echo "  tail -f ${INFRA_OUT} ${EXPERIMENT_OUT}"
echo ""
echo "Check for alerts:"
echo "  ls ${WATCHDOG_DIR}/*.ALERT 2>/dev/null && echo 'ALERT(S) ACTIVE' || echo 'no alerts'"
echo ""
echo "Stop:"
echo "  kill \$(cat ${WATCHDOG_DIR}/infra-watchdog.pid) \$(cat ${WATCHDOG_DIR}/experiment-watchdog.pid)"
