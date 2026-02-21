#!/usr/bin/env bash
set -euo pipefail

################################################################################
# Deploy DeathStarBench Social Network
# Uses the Helm chart from the DeathStarBench submodule
################################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DSB_CHART="$PROJECT_DIR/DeathStarBench/socialNetwork/helm-chart/socialnetwork"
NAMESPACE="social-network"

if [ ! -d "$DSB_CHART" ]; then
  echo "ERROR: DeathStarBench chart not found at $DSB_CHART"
  echo "Run: git submodule update --init --recursive"
  exit 1
fi

echo "=== Deploying DeathStarBench Social Network ==="
echo "Namespace: $NAMESPACE"
echo "Chart: $DSB_CHART"
echo ""

# Deploy with Helm
helm upgrade --install social-network "$DSB_CHART" \
  --namespace "$NAMESPACE" \
  --wait --timeout 10m

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
