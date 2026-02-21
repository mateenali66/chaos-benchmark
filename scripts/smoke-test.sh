#!/usr/bin/env bash
################################################################################
# Smoke Test - End-to-End Pipeline Validation
# Validates: cluster, RBAC, social graph, wrk2, chaos tools, Prometheus
# Usage: ./scripts/smoke-test.sh
################################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

export AWS_PROFILE=personal
NAMESPACE="social-network"

PASS=0
FAIL=0
TESTS=()

check() {
    local name="$1"
    local result="$2"
    if [[ "${result}" == "pass" ]]; then
        PASS=$((PASS + 1))
        TESTS+=("PASS: ${name}")
        echo "  PASS: ${name}"
    else
        FAIL=$((FAIL + 1))
        TESTS+=("FAIL: ${name}")
        echo "  FAIL: ${name}"
    fi
}

cleanup() {
    # Kill any port-forwards we started
    if [[ -n "${PF_PIDS:-}" ]]; then
        for pid in ${PF_PIDS}; do
            kill "${pid}" 2>/dev/null || true
        done
    fi
    # Clean up test resources
    kubectl delete -f "${PROJECT_ROOT}/experiments/chaos-mesh/p1-pod-kill.yaml" --ignore-not-found=true 2>/dev/null || true
    kubectl delete -f "${PROJECT_ROOT}/experiments/litmus/p1-pod-kill.yaml" --ignore-not-found=true 2>/dev/null || true
    kubectl delete job wrk2-smoke-test -n "${NAMESPACE}" --ignore-not-found=true 2>/dev/null || true
}
trap cleanup EXIT

PF_PIDS=""

echo "================================================================================"
echo "  Chaos Benchmark - Smoke Test"
echo "  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "================================================================================"

################################################################################
# Test 1: Cluster Connectivity
################################################################################
echo ""
echo "--- [1/7] Cluster Connectivity"

NODE_COUNT=$(kubectl get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ')
if [[ ${NODE_COUNT} -ge 2 ]]; then
    check "EKS nodes available (${NODE_COUNT})" "pass"
else
    check "EKS nodes available (${NODE_COUNT})" "fail"
fi

POD_COUNT=$(kubectl get pods -n "${NAMESPACE}" --no-headers 2>/dev/null | grep -c "Running" || echo "0")
if [[ ${POD_COUNT} -ge 20 ]]; then
    check "Social Network pods running (${POD_COUNT})" "pass"
else
    check "Social Network pods running (${POD_COUNT})" "fail"
fi

################################################################################
# Test 2: Litmus RBAC
################################################################################
echo ""
echo "--- [2/7] Litmus RBAC"

SA_EXISTS=$(kubectl get sa litmus-admin -n "${NAMESPACE}" -o name 2>/dev/null || echo "")
if [[ -n "${SA_EXISTS}" ]]; then
    check "litmus-admin ServiceAccount" "pass"
else
    check "litmus-admin ServiceAccount" "fail"
fi

EXP_COUNT=$(kubectl get chaosexperiments -n "${NAMESPACE}" --no-headers 2>/dev/null | wc -l | tr -d ' ')
if [[ ${EXP_COUNT} -ge 8 ]]; then
    check "ChaosExperiment CRDs installed (${EXP_COUNT})" "pass"
else
    check "ChaosExperiment CRDs installed (${EXP_COUNT})" "fail"
fi

################################################################################
# Test 3: Social Graph
################################################################################
echo ""
echo "--- [3/7] Social Graph"

kubectl port-forward svc/nginx-thrift 18081:8080 -n "${NAMESPACE}" &>/dev/null &
PF_PIDS="$! ${PF_PIDS:-}"
sleep 3

TIMELINE=$(curl -sf "http://localhost:18081/wrk2-api/user-timeline/read?user_id=1&start=0&stop=10" 2>/dev/null || echo "")
if [[ -n "${TIMELINE}" && "${TIMELINE}" != "[]" ]]; then
    check "Social graph data accessible" "pass"
else
    check "Social graph data accessible" "fail"
fi

################################################################################
# Test 4: Chaos Mesh
################################################################################
echo ""
echo "--- [4/7] Chaos Mesh"

CM_PODS=$(kubectl get pods -n chaos-testing --no-headers 2>/dev/null | grep -c "Running" || echo "0")
if [[ ${CM_PODS} -ge 3 ]]; then
    check "Chaos Mesh pods running (${CM_PODS})" "pass"
else
    check "Chaos Mesh pods running (${CM_PODS})" "fail"
fi

# Quick apply/delete test
kubectl apply -f "${PROJECT_ROOT}/experiments/chaos-mesh/p1-pod-kill.yaml" 2>/dev/null
sleep 3
CM_RESOURCE=$(kubectl get podchaos -n "${NAMESPACE}" --no-headers 2>/dev/null | wc -l | tr -d ' ')
kubectl delete -f "${PROJECT_ROOT}/experiments/chaos-mesh/p1-pod-kill.yaml" --ignore-not-found=true 2>/dev/null
if [[ ${CM_RESOURCE} -ge 1 ]]; then
    check "Chaos Mesh P1 apply/delete" "pass"
else
    check "Chaos Mesh P1 apply/delete" "fail"
fi

################################################################################
# Test 5: LitmusChaos
################################################################################
echo ""
echo "--- [5/7] LitmusChaos"

LITMUS_PODS=$(kubectl get pods -n litmus --no-headers 2>/dev/null | grep -c "Running" || echo "0")
if [[ ${LITMUS_PODS} -ge 3 ]]; then
    check "Litmus pods running (${LITMUS_PODS})" "pass"
else
    check "Litmus pods running (${LITMUS_PODS})" "fail"
fi

# Quick apply/delete test
kubectl apply -f "${PROJECT_ROOT}/experiments/litmus/p1-pod-kill.yaml" 2>/dev/null
sleep 3
LITMUS_RESOURCE=$(kubectl get chaosengine -n "${NAMESPACE}" --no-headers 2>/dev/null | wc -l | tr -d ' ')
kubectl delete -f "${PROJECT_ROOT}/experiments/litmus/p1-pod-kill.yaml" --ignore-not-found=true 2>/dev/null
if [[ ${LITMUS_RESOURCE} -ge 1 ]]; then
    check "Litmus P1 apply/delete" "pass"
else
    check "Litmus P1 apply/delete" "fail"
fi

################################################################################
# Test 6: Prometheus
################################################################################
echo ""
echo "--- [6/7] Prometheus"

# Kill any stale port-forward on this port
lsof -ti:19090 2>/dev/null | xargs kill 2>/dev/null || true
sleep 1

kubectl port-forward svc/prometheus-kube-prometheus-prometheus 19090:9090 -n monitoring &>/dev/null &
PF_PIDS="$! ${PF_PIDS:-}"
sleep 5

# Retry Prometheus connectivity (up to 3 attempts)
PROM_UP=""
for i in 1 2 3; do
    PROM_UP=$(curl -sf "http://localhost:19090/api/v1/query?query=up" 2>/dev/null || echo "")
    if [[ -n "${PROM_UP}" ]]; then break; fi
    sleep 3
done
if [[ -n "${PROM_UP}" && "${PROM_UP}" == *"success"* ]]; then
    check "Prometheus API reachable" "pass"
else
    check "Prometheus API reachable" "fail"
fi

PROM_CPU=$(curl -sf "http://localhost:19090/api/v1/query?query=container_cpu_usage_seconds_total%7Bnamespace%3D%22social-network%22%7D" 2>/dev/null || echo "")
if [[ -n "${PROM_CPU}" && "${PROM_CPU}" == *"result"* ]]; then
    check "Prometheus has container metrics" "pass"
else
    check "Prometheus has container metrics" "fail"
fi

################################################################################
# Test 7: Monitoring Stack
################################################################################
echo ""
echo "--- [7/7] Monitoring Stack"

GRAFANA_PODS=$(kubectl get pods -n monitoring -l app.kubernetes.io/name=grafana --no-headers 2>/dev/null | grep -c "Running" || echo "0")
if [[ ${GRAFANA_PODS} -ge 1 ]]; then
    check "Grafana running" "pass"
else
    check "Grafana running" "fail"
fi

JAEGER_PODS=$(kubectl get pods -n monitoring -l app=jaeger --no-headers 2>/dev/null | grep -c "Running" || echo "0")
if [[ ${JAEGER_PODS} -ge 1 ]]; then
    check "Jaeger running" "pass"
else
    check "Jaeger running" "fail"
fi

################################################################################
# Summary
################################################################################
TOTAL=$((PASS + FAIL))

echo ""
echo "================================================================================"
echo "  Smoke Test Results: ${PASS}/${TOTAL} passed"
echo "================================================================================"
for t in "${TESTS[@]}"; do
    echo "    ${t}"
done
echo "================================================================================"

if [[ ${FAIL} -gt 0 ]]; then
    echo "  STATUS: FAILED (${FAIL} test(s) failed)"
    exit 1
else
    echo "  STATUS: ALL PASSED"
    echo ""
    echo "  Ready to run experiments:"
    echo "    ./scripts/run-experiment.sh --tool chaos-mesh --scenario p1 --run 1"
    echo "    ./scripts/run-all-experiments.sh"
    exit 0
fi
