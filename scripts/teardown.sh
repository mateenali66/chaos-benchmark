#!/usr/bin/env bash
set -euo pipefail

################################################################################
# Cluster Teardown Script
# Run BEFORE: cd terraform && terraform destroy
# Cleans up Helm releases and CRDs to prevent stuck finalizers
################################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Teardown: Removing Helm releases and CRDs ==="
echo "This prevents stuck finalizers during terraform destroy"
echo ""

# Step 1: Remove Gremlin (if installed)
if helm status gremlin -n gremlin &>/dev/null; then
  echo "--- Uninstalling Gremlin ---"
  helm uninstall gremlin -n gremlin --wait --timeout 3m
fi

# Step 2: Remove LitmusChaos
if helm status litmus -n litmus &>/dev/null; then
  echo "--- Uninstalling LitmusChaos ---"
  helm uninstall litmus -n litmus --wait --timeout 3m

  echo "Removing Litmus CRDs..."
  kubectl get crd -o name | grep litmus 2>/dev/null | xargs -r kubectl delete --timeout=60s || true
fi

# Step 3: Remove Chaos Mesh (must delete experiments first)
if helm status chaos-mesh -n chaos-testing &>/dev/null; then
  echo "--- Cleaning up Chaos Mesh experiments ---"
  for kind in networkchaos podchaos stresschaos iochaos dnschaos httpchaos; do
    kubectl delete "$kind" --all --all-namespaces --timeout=30s 2>/dev/null || true
  done

  echo "--- Uninstalling Chaos Mesh ---"
  helm uninstall chaos-mesh -n chaos-testing --wait --timeout 3m

  echo "Removing Chaos Mesh CRDs..."
  kubectl get crd -o name | grep chaos-mesh 2>/dev/null | xargs -r kubectl delete --timeout=60s || true
fi

# Step 4: Remove DeathStarBench (if deployed)
if helm status social-network -n social-network &>/dev/null; then
  echo "--- Uninstalling DeathStarBench Social Network ---"
  helm uninstall social-network -n social-network --wait --timeout 3m
fi

# Step 5: Remove Prometheus + Grafana
if helm status prometheus -n monitoring &>/dev/null; then
  echo "--- Uninstalling Prometheus stack ---"
  helm uninstall prometheus -n monitoring --wait --timeout 3m

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
echo "=== Teardown Complete ==="
echo "You can now safely run: cd terraform && terraform destroy"
