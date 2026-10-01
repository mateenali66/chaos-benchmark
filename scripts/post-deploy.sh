#!/usr/bin/env bash
################################################################################
# Post-deploy setup. Run after setup.sh and deploy-dsb.sh. Steps:
#   1. Litmus RBAC (litmus-admin service account)
#   2. Litmus chaos operator and CRDs, if missing
#   3. ChaosExperiment definitions from ChaosHub
#   4. check the number of installed ChaosExperiments
#   5. social graph initialization (init-social-graph.sh)
#   6. check that a timeline read returns data
#
# Env: CHAOS_SLOT (default 0) targets that slot's namespace, which must already
# be deployed with deploy-dsb.sh (see scripts/SLOT_PARALLELISM.md).
# INSTALL_LITMUS=0 skips steps 2 and 3.
# Usage: ./scripts/post-deploy.sh
################################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

export AWS_PROFILE="${AWS_PROFILE:-default}"
# Exported so init-social-graph.sh uses the same slot.
export CHAOS_SLOT="${CHAOS_SLOT:-0}"

# Namespace for this slot. Keep in sync with chaoslib.namespace_for_slot().
if [[ -z "${CHAOS_SLOT}" || "${CHAOS_SLOT}" == "0" ]]; then
    NAMESPACE="social-network"
else
    NAMESPACE="social-network-${CHAOS_SLOT}"
fi

# ChaosExperiment types used by the 12 manifests in experiments/litmus/
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
if [[ -z "${CHAOS_SLOT}" || "${CHAOS_SLOT}" == "0" ]]; then
    kubectl apply -f "${PROJECT_ROOT}/manifests/litmus-rbac.yaml"
else
    # Slot namespaces get their own service account and a uniquely named
    # ClusterRoleBinding to the shared litmus-admin ClusterRole, which the
    # slot 0 run creates, so run slot 0 first. See
    # manifests/litmus-rbac-slot.yaml.tpl. deploy-dsb.sh explains the
    # mktemp -d with a fixed filename.
    SLOT_RBAC_TMPDIR="$(mktemp -d)"
    SLOT_RBAC_FILE="${SLOT_RBAC_TMPDIR}/litmus-rbac-slot.yaml"
    trap 'rm -rf "${SLOT_RBAC_TMPDIR}"' EXIT
    sed -e "s/\${NAMESPACE}/${NAMESPACE}/g" -e "s/\${CHAOS_SLOT}/${CHAOS_SLOT}/g" \
        "${PROJECT_ROOT}/manifests/litmus-rbac-slot.yaml.tpl" > "${SLOT_RBAC_FILE}"
    kubectl apply -f "${SLOT_RBAC_FILE}"
fi
echo "    SA: $(kubectl get sa litmus-admin -n "${NAMESPACE}" -o name 2>/dev/null || echo 'FAILED')"

################################################################################
# Step 2: Install Chaos Operator CRDs + Operator (if not present)
################################################################################
echo ""
# INSTALL_LITMUS=0 skips the operator, CRD and ChaosExperiment steps, so a
# cluster can be prepared with no chaos tool for the run-overhead.sh baseline
# stage.
INSTALL_LITMUS="${INSTALL_LITMUS:-1}"
if [ "$INSTALL_LITMUS" != "1" ]; then
  echo "--- [2/6] SKIPPING Litmus operator/CRDs (INSTALL_LITMUS=$INSTALL_LITMUS)"
fi
echo "--- [2/6] Installing Litmus Chaos Operator CRDs and operator..."

if [ "$INSTALL_LITMUS" = "1" ] && ! kubectl get crds chaosengines.litmuschaos.io &>/dev/null; then
    echo "    Installing CRDs..."
    kubectl apply -f https://raw.githubusercontent.com/litmuschaos/chaos-operator/master/deploy/chaos_crds.yaml
else
    echo "    CRDs already installed"
fi

if [ "$INSTALL_LITMUS" = "1" ] && ! kubectl get deploy litmus -n litmus &>/dev/null; then
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
[ "$INSTALL_LITMUS" != "1" ] && CHAOS_EXPERIMENTS=()
for exp in "${CHAOS_EXPERIMENTS[@]+"${CHAOS_EXPERIMENTS[@]}"}"; do
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

# Port 18080 plus 97 per slot (chaoslib.SLOT_PORT_STEP), so post-deploy runs
# for different slots of one cluster do not collide.
VERIFY_PORT=18080
if [[ -n "${CHAOS_SLOT}" && "${CHAOS_SLOT}" != "0" ]]; then
    VERIFY_PORT=$(( 18080 + CHAOS_SLOT * 97 ))
fi
kubectl port-forward svc/nginx-thrift "${VERIFY_PORT}:8080" -n "${NAMESPACE}" &>/dev/null &
PF_PID=$!
sleep 3

VERIFIED=false
if kill -0 "${PF_PID}" 2>/dev/null; then
    RESPONSE=$(curl -sf "http://localhost:${VERIFY_PORT}/wrk2-api/user-timeline/read?user_id=1&start=0&stop=10" 2>/dev/null || echo "")
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
