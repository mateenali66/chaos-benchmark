#!/usr/bin/env bash
################################################################################
# Initialize DeathStarBench Social Graph
# Registers 962 users, follows, and optionally composes posts via socfb-Reed98
# Requires: kubectl, python3, aiohttp
# Usage: ./scripts/init-social-graph.sh
#
# Slot parallelism: CHAOS_SLOT env var (default "0") selects which of the
# cluster's up-to-3 isolated namespaces to initialize. Unset/"0" is the
# original behavior, byte-for-byte. See scripts/SLOT_PARALLELISM.md.
################################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DSB_SOCIAL="${PROJECT_ROOT}/DeathStarBench/socialNetwork"
CHAOS_SLOT="${CHAOS_SLOT:-0}"

# Per-cluster local port: two clusters' resets overlap constantly under the
# per-rep reset protocol, and a fixed port makes their init port-forwards
# collide (bench-a baseline rep 6 failure). Derive a stable offset from the
# active kubeconfig so each cluster gets its own port with no env plumbing.
PORT_OFFSET=$(( $(printf '%s' "${KUBECONFIG:-default}" | cksum | cut -d' ' -f1) % 500 ))
# Slot-specific offset stacked on top of PORT_OFFSET (kept in sync with
# chaoslib.slot_port_offset() / chaoslib.SLOT_PORT_STEP and
# reset-app-state.sh, same 97-per-slot step), so 3 slots sharing one
# KUBECONFIG never collide on this port-forward either. Slot 0/unset adds 0
# (unchanged behavior).
SLOT_PORT_OFFSET=0
if [[ -n "${CHAOS_SLOT}" && "${CHAOS_SLOT}" != "0" ]]; then
    SLOT_PORT_OFFSET=$(( CHAOS_SLOT * 97 ))
fi
LOCAL_PORT=$(( 28080 + PORT_OFFSET + SLOT_PORT_OFFSET ))

# Namespace convention for slot parallelism (slot 0/unset = default
# namespace; slot N = "social-network-N"). Kept in sync with
# chaoslib.namespace_for_slot() and reset-app-state.sh -- if you change
# this logic, change it in all three places.
if [[ -z "${CHAOS_SLOT}" || "${CHAOS_SLOT}" == "0" ]]; then
    NAMESPACE="social-network"
else
    NAMESPACE="social-network-${CHAOS_SLOT}"
fi
SERVICE="svc/nginx-thrift"
PF_PID=""

cleanup() {
    if [[ -n "${PF_PID}" ]] && kill -0 "${PF_PID}" 2>/dev/null; then
        echo "--- Cleaning up port-forward (PID: ${PF_PID})..."
        kill "${PF_PID}" 2>/dev/null || true
        wait "${PF_PID}" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# Check prerequisites
if ! command -v kubectl &>/dev/null; then
    echo "ERROR: kubectl not found" >&2
    exit 1
fi
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found" >&2
    exit 1
fi

# Set up venv for aiohttp dependency
VENV_DIR="${PROJECT_ROOT}/.venv"
if [[ ! -d "${VENV_DIR}" ]]; then
    echo "--- Creating Python virtual environment..."
    python3 -m venv "${VENV_DIR}"
fi
source "${VENV_DIR}/bin/activate"

if ! python3 -c "import aiohttp" 2>/dev/null; then
    echo "--- Installing aiohttp..."
    pip3 install aiohttp
fi

# Check if social graph is already initialized (idempotent)
echo "--- Checking if social graph is already initialized..."
kubectl port-forward "${SERVICE}" "${LOCAL_PORT}:8080" -n "${NAMESPACE}" &>/dev/null &
PF_PID=$!
sleep 3

# Verify port-forward is alive
if ! kill -0 "${PF_PID}" 2>/dev/null; then
    echo "ERROR: Port-forward failed to start. Is ${SERVICE} running in ${NAMESPACE}?" >&2
    exit 1
fi

# Test connectivity
for i in $(seq 1 10); do
    if curl -sf "http://localhost:${LOCAL_PORT}/" &>/dev/null; then
        break
    fi
    if [[ $i -eq 10 ]]; then
        echo "ERROR: Cannot reach ${SERVICE} on port ${LOCAL_PORT}" >&2
        exit 1
    fi
    sleep 2
done

# Check if users already registered by querying user_id=1
RESPONSE=$(curl -sf "http://localhost:${LOCAL_PORT}/wrk2-api/user-timeline/read?user_id=1&start=0&stop=10" 2>/dev/null || echo "")
if [[ -n "${RESPONSE}" && "${RESPONSE}" != "[]" && "${RESPONSE}" != *"error"* ]]; then
    echo "--- Social graph appears already initialized (user_id=1 has data). Skipping."
    exit 0
fi

# Kill the test port-forward; init script will use the same port
kill "${PF_PID}" 2>/dev/null || true
wait "${PF_PID}" 2>/dev/null || true
sleep 1

# Start fresh port-forward for init
kubectl port-forward "${SERVICE}" "${LOCAL_PORT}:8080" -n "${NAMESPACE}" &>/dev/null &
PF_PID=$!
sleep 3

if ! kill -0 "${PF_PID}" 2>/dev/null; then
    echo "ERROR: Port-forward failed to restart" >&2
    exit 1
fi

# Run init_social_graph.py from the socialNetwork directory (dataset paths are relative)
echo "=== Initializing social graph (socfb-Reed98, 962 users) ==="
echo "    This takes 2-5 minutes depending on cluster performance..."
cd "${DSB_SOCIAL}"
python3 scripts/init_social_graph.py \
    --graph socfb-Reed98 \
    --ip 127.0.0.1 \
    --port "${LOCAL_PORT}" \
    --compose

echo "=== Social graph initialized successfully ==="
