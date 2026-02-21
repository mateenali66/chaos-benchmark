#!/usr/bin/env bash
################################################################################
# Post-Deploy Setup
# Orchestrates: Litmus RBAC, ChaosExperiment CRDs, social graph initialization
# Run this AFTER setup.sh + deploy-dsb.sh have completed successfully.
# Usage: ./scripts/post-deploy.sh
################################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

export AWS_PROFILE=personal
NAMESPACE="social-network"

# ChaosExperiment types referenced by our 12 Litmus experiment YAMLs
CHAOS_EXPERIMENTS=(
    "pod-delete"
    "container-kill"
    "pod-network-latency"
    "pod-network-loss"
    "pod-network-partition"
    "pod-cpu-hog-exec"
    "pod-memory-hog-exec"
    "pod-http-status-code"
)

# Litmus Hub API for experiment definitions (3.x uses faults/kubernetes/ path)
CHAOSHUB_BASE="https://hub.litmuschaos.io/api/chaos/3.0.0?file=faults/kubernetes"

echo "================================================================================"
echo "  Post-Deploy Setup for Chaos Benchmark"
echo "================================================================================"

################################################################################
# Step 1: Apply Litmus RBAC
################################################################################
echo ""
echo "--- [1/6] Applying Litmus RBAC (litmus-admin SA in ${NAMESPACE})..."
kubectl apply -f "${PROJECT_ROOT}/manifests/litmus-rbac.yaml"
echo "    SA: $(kubectl get sa litmus-admin -n "${NAMESPACE}" -o name 2>/dev/null || echo 'FAILED')"

################################################################################
# Step 2: Install Chaos Operator CRDs + Operator (if not present)
################################################################################
echo ""
echo "--- [2/6] Installing Litmus Chaos Operator CRDs and operator..."

if ! kubectl get crds chaosengines.litmuschaos.io &>/dev/null; then
    echo "    Installing CRDs..."
    kubectl apply -f https://raw.githubusercontent.com/litmuschaos/chaos-operator/master/deploy/chaos_crds.yaml
else
    echo "    CRDs already installed"
fi

if ! kubectl get deploy litmus -n litmus &>/dev/null; then
    echo "    Installing operator RBAC..."
    kubectl apply -f https://raw.githubusercontent.com/litmuschaos/chaos-operator/master/deploy/rbac.yaml
    echo "    Installing operator..."
    kubectl apply -f https://raw.githubusercontent.com/litmuschaos/chaos-operator/master/deploy/operator.yaml -n litmus
    echo "    Waiting for operator to start..."
    kubectl rollout status deploy/litmus -n litmus --timeout=60s 2>/dev/null || true
else
    echo "    Chaos operator already running"
fi

################################################################################
# Step 3: Install ChaosExperiment definitions
################################################################################
echo ""
echo "--- [3/6] Installing ChaosExperiment definitions (${#CHAOS_EXPERIMENTS[@]} experiments)..."

INSTALL_FAILURES=0
for exp in "${CHAOS_EXPERIMENTS[@]}"; do
    EXP_URL="${CHAOSHUB_BASE}/${exp}/fault.yaml"

    echo "    Installing: ${exp}..."
    if ! kubectl apply -f "${EXP_URL}" -n "${NAMESPACE}" 2>/dev/null; then
        echo "    WARNING: Failed to install experiment CRD for ${exp}" >&2
        INSTALL_FAILURES=$((INSTALL_FAILURES + 1))
    fi
done

if [[ ${INSTALL_FAILURES} -gt 0 ]]; then
    echo "    WARNING: ${INSTALL_FAILURES} experiment(s) failed to install."
    echo "    You may need to install them manually from ${CHAOSHUB_BASE}"
fi

################################################################################
# Step 4: Verify ChaosExperiment installation
################################################################################
echo ""
echo "--- [4/6] Verifying ChaosExperiment installation..."
INSTALLED=$(kubectl get chaosexperiments -n "${NAMESPACE}" --no-headers 2>/dev/null | wc -l | tr -d ' ')
echo "    Installed: ${INSTALLED} ChaosExperiments in ${NAMESPACE}"

if [[ ${INSTALLED} -lt ${#CHAOS_EXPERIMENTS[@]} ]]; then
    echo "    WARNING: Expected ${#CHAOS_EXPERIMENTS[@]}, got ${INSTALLED}"
    kubectl get chaosexperiments -n "${NAMESPACE}" 2>/dev/null || true
fi

################################################################################
# Step 5: Initialize social graph
################################################################################
echo ""
echo "--- [5/6] Initializing social graph..."
chmod +x "${SCRIPT_DIR}/init-social-graph.sh"
"${SCRIPT_DIR}/init-social-graph.sh"

################################################################################
# Step 6: Verify social graph
################################################################################
echo ""
echo "--- [6/6] Verifying social graph..."

# Start a quick port-forward to test
kubectl port-forward svc/nginx-thrift 18080:8080 -n "${NAMESPACE}" &>/dev/null &
PF_PID=$!
sleep 3

VERIFIED=false
if kill -0 "${PF_PID}" 2>/dev/null; then
    RESPONSE=$(curl -sf "http://localhost:18080/wrk2-api/user-timeline/read?user_id=1&start=0&stop=10" 2>/dev/null || echo "")
    if [[ -n "${RESPONSE}" && "${RESPONSE}" != "[]" ]]; then
        echo "    Social graph verified: user_id=1 has timeline data"
        VERIFIED=true
    else
        echo "    WARNING: user_id=1 returned empty/no data. Graph may not be initialized."
    fi
    kill "${PF_PID}" 2>/dev/null || true
    wait "${PF_PID}" 2>/dev/null || true
else
    echo "    WARNING: Could not verify social graph (port-forward failed)"
fi

################################################################################
# Summary
################################################################################
echo ""
echo "================================================================================"
echo "  Post-Deploy Summary"
echo "================================================================================"
echo "  Litmus RBAC:       $(kubectl get sa litmus-admin -n "${NAMESPACE}" -o name 2>/dev/null && echo 'OK' || echo 'FAILED')"
echo "  ChaosExperiments:  ${INSTALLED}/${#CHAOS_EXPERIMENTS[@]}"
echo "  Social Graph:      $(${VERIFIED} && echo 'Verified' || echo 'Unverified')"
echo ""
echo "  Next steps:"
echo "    1. Build wrk2 image:  ./scripts/build-wrk2-image.sh"
echo "    2. Run smoke test:    ./scripts/smoke-test.sh"
echo "    3. Run experiments:   ./scripts/run-experiment.sh --tool chaos-mesh --scenario p1 --run 1"
echo "================================================================================"
