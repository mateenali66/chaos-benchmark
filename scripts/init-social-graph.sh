#!/usr/bin/env bash
################################################################################
# Initialize DeathStarBench Social Graph
# Registers 962 users, follows, and optionally composes posts via socfb-Reed98
# Requires: kubectl, python3, aiohttp
# Usage: ./scripts/init-social-graph.sh
################################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DSB_SOCIAL="${PROJECT_ROOT}/DeathStarBench/socialNetwork"

LOCAL_PORT=8080
NAMESPACE="social-network"
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
