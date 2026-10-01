#!/usr/bin/env bash
set -euo pipefail

################################################################################
# Start infra-watchdog.py and experiment-watchdog.py in the background (nohup)
# for one cluster. Status files, output and pidfiles go to
# ${CHAOS_DATA_DIR:-data}/watchdogs/, and the experiment watchdog follows
# ${CHAOS_DATA_DIR:-data}/progress.log. For the data-v2 layout, set
# CHAOS_DATA_DIR to the cluster's data-v2/<env> directory. campaign-watchdog.py
# and s3-sync-loop.sh are not started here.
#
# Usage: ./scripts/watchdogs/watch-all.sh <bench-a|bench-b|ml> [--stall-minutes N]
# The stall threshold defaults to 25 minutes.
#
# The experiment watchdog checks that the runner process is alive only if a
# pidfile is written when run-all-experiments.sh is launched. Write it before
# running this script, e.g.:
#   mkdir -p "$CHAOS_DATA_DIR/watchdogs"
#   nohup ./scripts/run-all-experiments.sh --tool chaos-mesh > "$CHAOS_DATA_DIR/watchdogs/run-all.log" 2>&1 &
#   echo $! > "$CHAOS_DATA_DIR/watchdogs/run-all.pid"
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
