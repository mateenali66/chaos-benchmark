#!/usr/bin/env bash
set -euo pipefail

################################################################################
# Deploy DeathStarBench Social Network
# Uses the Helm chart from the DeathStarBench submodule
#
# Slot parallelism: pass a slot ("1" or "2") as the first positional arg, or
# set CHAOS_SLOT in the environment, to deploy into an isolated namespace
# (social-network-<slot>) with a nodeSelector overlay pinning it to that
# slot's dedicated nodes (node label chaos-slot=<slot>, provisioned
# separately -- see scripts/SLOT_PARALLELISM.md). Slot 0/unset (the default)
# deploys exactly as before: namespace social-network, no nodeSelector, no
# overlay file -- byte-for-byte the original behavior.
#
# Usage:
#   ./scripts/deploy-dsb.sh          # default: namespace social-network
#   ./scripts/deploy-dsb.sh 1        # namespace social-network-1
#   CHAOS_SLOT=2 ./scripts/deploy-dsb.sh
################################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DSB_CHART="$PROJECT_DIR/DeathStarBench/socialNetwork/helm-chart/socialnetwork"

# Positional arg (if given) wins over the CHAOS_SLOT env var.
CHAOS_SLOT="${1:-${CHAOS_SLOT:-0}}"

# Namespace convention for slot parallelism (slot 0/unset = default
# namespace; slot N = "social-network-N"). Kept in sync with
# chaoslib.namespace_for_slot() -- if you change this logic, change it
# there too.
if [[ -z "${CHAOS_SLOT}" || "${CHAOS_SLOT}" == "0" ]]; then
    NAMESPACE="social-network"
else
    NAMESPACE="social-network-${CHAOS_SLOT}"
fi

if [ ! -d "$DSB_CHART" ]; then
  echo "ERROR: DeathStarBench chart not found at $DSB_CHART"
  echo "Run: git submodule update --init --recursive"
  exit 1
fi

echo "=== Deploying DeathStarBench Social Network ==="
echo "Namespace: $NAMESPACE"
echo "Chart: $DSB_CHART"
echo ""

HELM_VALUES_ARGS=(--values "$PROJECT_DIR/helm/dsb-values.yaml")

if [[ -n "${CHAOS_SLOT}" && "${CHAOS_SLOT}" != "0" ]]; then
    echo "Slot: ${CHAOS_SLOT} (nodeSelector chaos-slot=${CHAOS_SLOT})"

    kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1 || kubectl create namespace "${NAMESPACE}"

    # mktemp's "template-XXXXXX.suffix" form does not reliably substitute in
    # this environment (reproducible collision across concurrent invocations);
    # mktemp -d randomizes correctly, so use a fixed filename inside a
    # randomized directory instead.
    SLOT_TMPDIR="$(mktemp -d)"
    SLOT_VALUES_FILE="${SLOT_TMPDIR}/dsb-values-slot.yaml"
    trap 'rm -rf "${SLOT_TMPDIR}"' EXIT
    sed "s/\${CHAOS_SLOT}/${CHAOS_SLOT}/g" "$PROJECT_DIR/helm/dsb-values-slot.yaml.tpl" > "${SLOT_VALUES_FILE}"
    HELM_VALUES_ARGS+=(--values "${SLOT_VALUES_FILE}")
    echo "Slot overlay rendered to: ${SLOT_VALUES_FILE}"
    echo ""
fi

# Deploy with Helm
helm upgrade --install social-network "$DSB_CHART" \
  --namespace "$NAMESPACE" \
  "${HELM_VALUES_ARGS[@]}" \
  --wait --timeout 10m

# The vendored chart templates do not read any nodeSelector key (verified:
# zero hits for nodeSelector in templates/), so dsb-values-slot.yaml.tpl's
# overlay above is otherwise a no-op. Node pinning for slot!=0 is instead
# applied here via a post-install patch of every Deployment in the slot's
# namespace -- this avoids modifying the vendored third-party chart. The
# patch triggers a rolling update, so pods are re-waited afterward.
if [[ -n "${CHAOS_SLOT}" && "${CHAOS_SLOT}" != "0" ]]; then
    echo ""
    echo "=== Pinning slot ${CHAOS_SLOT} workloads to chaos-slot=${CHAOS_SLOT} nodes ==="
    for dep in $(kubectl get deployments -n "$NAMESPACE" -o name); do
        kubectl patch "$dep" -n "$NAMESPACE" --type merge \
            -p "{\"spec\":{\"template\":{\"spec\":{\"nodeSelector\":{\"chaos-slot\":\"${CHAOS_SLOT}\"}}}}}" > /dev/null
    done
    echo "Waiting for patched deployments to roll out onto slot ${CHAOS_SLOT} nodes..."
    for dep in $(kubectl get deployments -n "$NAMESPACE" -o name); do
        kubectl rollout status "$dep" -n "$NAMESPACE" --timeout=180s
    done
fi

echo ""
echo "=== Verifying deployment ==="
kubectl -n "$NAMESPACE" get pods
echo ""

echo "Waiting for all pods to be ready..."
kubectl -n "$NAMESPACE" wait --for=condition=Ready pods --all --timeout=300s

echo ""
echo "=== Social Network deployed ==="
echo ""
echo "Initialize social graph (run from DSB container):"
echo "  kubectl -n $NAMESPACE exec -it deploy/social-network-nginx-thrift -- bash"
echo "  python3 scripts/init_social_graph.py"
echo ""
echo "Test API:"
echo "  kubectl -n $NAMESPACE port-forward svc/nginx-thrift 8080:8080"
echo "  curl http://localhost:8080"
