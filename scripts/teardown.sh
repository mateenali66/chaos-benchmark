#!/usr/bin/env bash
set -euo pipefail

################################################################################
# Cluster Teardown Script
# Run BEFORE: terraform -chdir=terraform destroy -var-file=envs/<env>.tfvars
# Cleans up Helm releases and CRDs to prevent stuck finalizers
#
# Usage: ./scripts/teardown.sh <bench-a|bench-b|ml>
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

KUBE_CONTEXT="is-chaos-${ENV_NAME}"

echo "=== Teardown: ${CLUSTER_NAME} (kube context: ${KUBE_CONTEXT}) ==="
echo "Removing Helm releases and CRDs to prevent stuck finalizers"
echo ""

echo "--- Selecting Terraform workspace '${ENV_NAME}' (for the destroy step after this script) ---"
terraform -chdir="$TERRAFORM_DIR" workspace select "$ENV_NAME" 2>/dev/null \
  || echo "    WARNING: workspace '${ENV_NAME}' does not exist yet; nothing to destroy there."
echo ""

echo "--- Switching kubectl context to ${KUBE_CONTEXT} ---"
if ! kubectl config get-contexts "$KUBE_CONTEXT" &>/dev/null; then
  echo "ERROR: kube context '${KUBE_CONTEXT}' not found. Run ./scripts/setup.sh ${ENV_NAME} first (or the cluster is already gone)." >&2
  exit 1
fi
kubectl config use-context "$KUBE_CONTEXT"
echo ""

# Step 1: Remove Gremlin (if installed)
if helm status gremlin -n gremlin --kube-context "$KUBE_CONTEXT" &>/dev/null; then
  echo "--- Uninstalling Gremlin ---"
  helm uninstall gremlin -n gremlin --kube-context "$KUBE_CONTEXT" --wait --timeout 3m
fi

# Step 2: Remove LitmusChaos
if helm status litmus -n litmus --kube-context "$KUBE_CONTEXT" &>/dev/null; then
  echo "--- Uninstalling LitmusChaos ---"
  helm uninstall litmus -n litmus --kube-context "$KUBE_CONTEXT" --wait --timeout 3m

  echo "Removing Litmus CRDs..."
  kubectl get crd -o name | grep litmus 2>/dev/null | xargs -r kubectl delete --timeout=60s || true
fi

# Step 3: Remove Chaos Mesh (must delete experiments first)
if helm status chaos-mesh -n chaos-testing --kube-context "$KUBE_CONTEXT" &>/dev/null; then
  echo "--- Cleaning up Chaos Mesh experiments ---"
  for kind in networkchaos podchaos stresschaos iochaos dnschaos httpchaos; do
    kubectl delete "$kind" --all --all-namespaces --timeout=30s 2>/dev/null || true
  done

  echo "--- Uninstalling Chaos Mesh ---"
  helm uninstall chaos-mesh -n chaos-testing --kube-context "$KUBE_CONTEXT" --wait --timeout 3m

  echo "Removing Chaos Mesh CRDs..."
  kubectl get crd -o name | grep chaos-mesh 2>/dev/null | xargs -r kubectl delete --timeout=60s || true
fi

# Step 4: Remove DeathStarBench (if deployed)
if helm status social-network -n social-network --kube-context "$KUBE_CONTEXT" &>/dev/null; then
  echo "--- Uninstalling DeathStarBench Social Network ---"
  helm uninstall social-network -n social-network --kube-context "$KUBE_CONTEXT" --wait --timeout 3m
fi

# Step 5: Remove Prometheus + Grafana
if helm status prometheus -n monitoring --kube-context "$KUBE_CONTEXT" &>/dev/null; then
  echo "--- Uninstalling Prometheus stack ---"
  helm uninstall prometheus -n monitoring --kube-context "$KUBE_CONTEXT" --wait --timeout 3m

  echo "Removing Prometheus CRDs..."
  kubectl get crd -o name | grep monitoring.coreos.com 2>/dev/null | xargs -r kubectl delete --timeout=60s || true
fi

# Step 6: Remove Jaeger
echo "--- Removing Jaeger ---"
kubectl delete -f "$PROJECT_DIR/manifests/jaeger.yaml" --ignore-not-found --timeout=60s

# Step 7: Delete PVCs to release EBS volumes
echo "--- Deleting PVCs ---"
kubectl delete pvc --all -n monitoring --timeout=60s 2>/dev/null || true
kubectl delete pvc --all -n litmus --timeout=60s 2>/dev/null || true

# Step 8: Remove namespaces
echo "--- Removing namespaces ---"
kubectl delete -f "$PROJECT_DIR/manifests/namespaces.yaml" --ignore-not-found --timeout=120s

echo ""
echo "=== Teardown Complete for ${CLUSTER_NAME} ==="
echo "You can now safely run:"
echo "  terraform -chdir=terraform destroy -var-file=envs/${ENV_NAME}.tfvars"
echo ""
echo "NOTE: if this is bench-a and create_s3_bucket=true in envs/bench-a.tfvars,"
echo "destroying it also destroys the SHARED is-chaos-artifacts bucket used by"
echo "bench-b and ml. Export/backup data from all three clusters first, and"
echo "tear down bench-a last."
