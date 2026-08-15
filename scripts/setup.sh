#!/usr/bin/env bash
set -euo pipefail

################################################################################
# Cluster Bootstrap Script
# Run after: terraform -chdir=terraform apply -var-file=envs/<env>.tfvars
# Installs monitoring stack, chaos tools, and verifies cluster readiness
#
# Usage: ./scripts/setup.sh <bench-a|bench-b|ml>
################################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TERRAFORM_DIR="$PROJECT_DIR/terraform"

ENV_NAME="${1:-}"
case "$ENV_NAME" in
  bench-a) CLUSTER_NAME="is-chaos-bench-a" ;;
  bench-b) CLUSTER_NAME="is-chaos-bench-b" ;;
  ml)      CLUSTER_NAME="is-chaos-ml" ;;
  *)
    echo "Usage: $0 <bench-a|bench-b|ml>" >&2
    echo "" >&2
    echo "  bench-a  -> cluster is-chaos-bench-a  (terraform/envs/bench-a.tfvars)" >&2
    echo "  bench-b  -> cluster is-chaos-bench-b  (terraform/envs/bench-b.tfvars)" >&2
    echo "  ml       -> cluster is-chaos-ml       (terraform/envs/ml.tfvars)" >&2
    exit 1
    ;;
esac

AWS_PROFILE="${AWS_PROFILE:-personal}"
REGION="${AWS_REGION:-ca-central-1}"
KUBE_CONTEXT="is-chaos-${ENV_NAME}"

echo "=== Step 0/9: Select Terraform workspace '${ENV_NAME}' ==="
terraform -chdir="$TERRAFORM_DIR" workspace select "$ENV_NAME" 2>/dev/null \
  || terraform -chdir="$TERRAFORM_DIR" workspace new "$ENV_NAME"
echo ""

echo "=== Step 1/9: Update kubeconfig ==="
aws eks update-kubeconfig \
  --name "$CLUSTER_NAME" \
  --region "$REGION" \
  --profile "$AWS_PROFILE" \
  --alias "$KUBE_CONTEXT"
kubectl config use-context "$KUBE_CONTEXT"

echo "=== Step 2/9: Verify nodes are Ready ==="
echo "Waiting for nodes..."
kubectl wait --for=condition=Ready nodes --all --timeout=300s
kubectl get nodes -o wide
echo ""

echo "=== Step 3/9: Create namespaces ==="
kubectl apply -f "$PROJECT_DIR/manifests/namespaces.yaml"
echo ""

echo "=== Step 4/9: Deploy Jaeger (tracing) ==="
kubectl apply -f "$PROJECT_DIR/manifests/jaeger.yaml"
kubectl -n monitoring rollout status deployment/jaeger --timeout=120s
echo ""

echo "=== Step 5/9: Install Prometheus + Grafana ==="
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
helm repo update prometheus-community
helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --values "$PROJECT_DIR/helm/prometheus-values.yaml" \
  --wait --timeout 5m
echo ""

# Per-cluster tool assignment for the revised study: each cluster gets ONE
# chaos tool (bench-a=chaos-mesh, bench-b=litmus, ml=litmus). The overhead
# study's no-tool baseline runs (run-overhead.sh stage a) require the tool to
# NOT be installed yet, so pass SETUP_TOOLS=none for the initial bootstrap and
# re-run setup.sh with SETUP_TOOLS unset (or =<tool>) after stage (a) is done.
case "$ENV_NAME" in
  bench-a) DEFAULT_TOOLS="chaos-mesh" ;;
  bench-b) DEFAULT_TOOLS="litmus" ;;
  ml)      DEFAULT_TOOLS="litmus" ;;
esac
SETUP_TOOLS="${SETUP_TOOLS:-$DEFAULT_TOOLS}"

if [ "$SETUP_TOOLS" = "chaos-mesh" ] || [ "$SETUP_TOOLS" = "both" ]; then
  echo "=== Step 6/9: Install Chaos Mesh ==="
  helm repo add chaos-mesh https://charts.chaos-mesh.org 2>/dev/null || true
  helm repo update chaos-mesh
  helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh \
    --namespace chaos-testing \
    --values "$PROJECT_DIR/helm/chaos-mesh-values.yaml" \
    --wait --timeout 5m
  echo ""
else
  echo "=== Step 6/9: Skipping Chaos Mesh (SETUP_TOOLS=$SETUP_TOOLS) ==="
fi

if [ "$SETUP_TOOLS" = "litmus" ] || [ "$SETUP_TOOLS" = "both" ]; then
  echo "=== Step 7/9: Install LitmusChaos ==="
  helm repo add litmuschaos https://litmuschaos.github.io/litmus-helm/ 2>/dev/null || true
  helm repo update litmuschaos
  helm upgrade --install litmus litmuschaos/litmus \
    --namespace litmus \
    --values "$PROJECT_DIR/helm/litmus-values.yaml" \
    --wait --timeout 5m
  echo ""
else
  echo "=== Step 7/9: Skipping LitmusChaos (SETUP_TOOLS=$SETUP_TOOLS) ==="
fi

echo "=== Step 8/9: Install Gremlin (optional) ==="
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

echo "=== Step 9/9: Done ==="
echo ""
echo "Cluster:        $CLUSTER_NAME"
echo "Kube context:   $KUBE_CONTEXT"
echo "TF workspace:   $ENV_NAME"
echo ""
echo "Verify all pods:"
echo "  kubectl --context $KUBE_CONTEXT get pods -A"
echo ""
echo "Access dashboards:"
echo "  ./scripts/port-forward.sh"
echo ""
echo "Deploy DeathStarBench:"
echo "  ./scripts/deploy-dsb.sh"
echo ""
echo "Start watchdogs for this cluster:"
echo "  ./scripts/watchdogs/watch-all.sh $ENV_NAME"
