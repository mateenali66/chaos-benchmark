#!/usr/bin/env bash
set -euo pipefail

################################################################################
# Deploy the DeathStarBench Social Network with the Helm chart from the
# DeathStarBench submodule and the values in helm/dsb-values.yaml.
#
# Slot parallelism (see scripts/SLOT_PARALLELISM.md): pass a slot number as the
# first argument, or set CHAOS_SLOT, to deploy into namespace
# social-network-<slot> and pin its pods to nodes labelled chaos-slot=<slot>.
# The node labels must already exist. Slot 0 or unset deploys into namespace
# social-network with no node pinning.
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

# Namespace for this slot. Keep in sync with chaoslib.namespace_for_slot().
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

    # Use a fixed filename inside a mktemp -d directory. Some mktemp
    # implementations only randomize trailing Xs, so a name-XXXXXX.yaml
    # template can collide between concurrent runs.
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

# The vendored chart templates do not read nodeSelector, so the
# dsb-values-slot.yaml.tpl overlay above has no effect. For slots other than 0,
# pin the pods by patching a nodeSelector into every Deployment in the
# namespace, which leaves the third-party chart unmodified. The patch triggers
# a rolling update, so wait for it to finish.
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
