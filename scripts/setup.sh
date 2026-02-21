#!/usr/bin/env bash
set -euo pipefail

################################################################################
# Cluster Bootstrap Script
# Run after: cd terraform && terraform apply
# Installs monitoring stack, chaos tools, and verifies cluster readiness
################################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CLUSTER_NAME="${CLUSTER_NAME:-chaos-benchmark}"
AWS_PROFILE="${AWS_PROFILE:-personal}"
REGION="${AWS_REGION:-us-east-1}"

echo "=== Step 1/8: Update kubeconfig ==="
aws eks update-kubeconfig \
  --name "$CLUSTER_NAME" \
  --region "$REGION" \
  --profile "$AWS_PROFILE"

echo "=== Step 2/8: Verify nodes are Ready ==="
echo "Waiting for nodes..."
kubectl wait --for=condition=Ready nodes --all --timeout=300s
kubectl get nodes -o wide
echo ""

echo "=== Step 3/8: Create namespaces ==="
kubectl apply -f "$PROJECT_DIR/manifests/namespaces.yaml"
echo ""

echo "=== Step 4/8: Deploy Jaeger (tracing) ==="
kubectl apply -f "$PROJECT_DIR/manifests/jaeger.yaml"
kubectl -n monitoring rollout status deployment/jaeger --timeout=120s
echo ""

echo "=== Step 5/8: Install Prometheus + Grafana ==="
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
helm repo update prometheus-community
helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --values "$PROJECT_DIR/helm/prometheus-values.yaml" \
  --wait --timeout 5m
echo ""

echo "=== Step 6/8: Install Chaos Mesh ==="
helm repo add chaos-mesh https://charts.chaos-mesh.org 2>/dev/null || true
helm repo update chaos-mesh
helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh \
  --namespace chaos-testing \
  --values "$PROJECT_DIR/helm/chaos-mesh-values.yaml" \
  --wait --timeout 5m
echo ""

echo "=== Step 7/8: Install LitmusChaos ==="
helm repo add litmuschaos https://litmuschaos.github.io/litmus-helm/ 2>/dev/null || true
helm repo update litmuschaos
helm upgrade --install litmus litmuschaos/litmus \
  --namespace litmus \
  --values "$PROJECT_DIR/helm/litmus-values.yaml" \
  --wait --timeout 5m
echo ""

echo "=== Step 8/8: Install Gremlin (optional) ==="
if [ -n "${GREMLIN_TEAM_ID:-}" ] && [ -n "${GREMLIN_TEAM_SECRET:-}" ]; then
  helm repo add gremlin https://helm.gremlin.com 2>/dev/null || true
  helm repo update gremlin
  helm upgrade --install gremlin gremlin/gremlin \
    --namespace gremlin \
    --values "$PROJECT_DIR/helm/gremlin-values.yaml" \
    --set gremlin.secret.managed=true \
    --set gremlin.secret.teamID="$GREMLIN_TEAM_ID" \
    --set gremlin.secret.teamSecret="$GREMLIN_TEAM_SECRET" \
    --set gremlin.secret.clusterID="$CLUSTER_NAME" \
    --wait --timeout 5m
  echo ""
else
  echo "Skipping Gremlin: set GREMLIN_TEAM_ID and GREMLIN_TEAM_SECRET to install"
  echo ""
fi

echo "=== Setup Complete ==="
echo ""
echo "Verify all pods:"
echo "  kubectl get pods -A"
echo ""
echo "Access dashboards:"
echo "  ./scripts/port-forward.sh"
echo ""
echo "Deploy DeathStarBench:"
echo "  ./scripts/deploy-dsb.sh"
