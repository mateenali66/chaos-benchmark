#!/usr/bin/env bash
set -euo pipefail

################################################################################
# Export Experiment Data to S3
# Syncs local data/ directory to the shared is-chaos-artifacts bucket, keyed
# under the cluster's own name so all three concurrent clusters
# (is-chaos-bench-a, is-chaos-bench-b, is-chaos-ml) can write to the same
# bucket without clobbering each other.
#
# Usage: ./scripts/export-to-s3.sh <bench-a|bench-b|ml>
################################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TERRAFORM_DIR="$PROJECT_DIR/terraform"
DATA_DIR="$PROJECT_DIR/data"

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
AWS_REGION="${AWS_REGION:-ca-central-1}"

echo "--- Selecting Terraform workspace '${ENV_NAME}' ---"
terraform -chdir="$TERRAFORM_DIR" workspace select "$ENV_NAME"

# Get S3 bucket from Terraform output. All three workspaces resolve to the
# same bucket name (is-chaos-artifacts-<account_id>) whether or not this
# workspace is the one that owns/creates it (see terraform/main.tf locals).
BUCKET=$(cd "$TERRAFORM_DIR" && terraform output -raw s3_bucket 2>/dev/null)

if [ -z "$BUCKET" ]; then
  echo "ERROR: Could not get S3 bucket name from Terraform output." >&2
  echo "Make sure terraform has been applied for workspace '${ENV_NAME}':" >&2
  echo "  terraform -chdir=terraform apply -var-file=envs/${ENV_NAME}.tfvars" >&2
  exit 1
fi

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
DEST="s3://${BUCKET}/${CLUSTER_NAME}/data/${TIMESTAMP}/"

echo "=== Exporting experiment data to S3 ==="
echo "Cluster:     $CLUSTER_NAME"
echo "Source:      $DATA_DIR"
echo "Destination: $DEST"
echo "Profile:     $AWS_PROFILE"
echo "Region:      $AWS_REGION"
echo ""

# Sync data directory (key-prefixed by cluster name so all three clusters can
# share the one bucket)
aws s3 sync "$DATA_DIR" "$DEST" \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" \
  --exclude ".gitkeep"

echo ""
echo "=== Prometheus snapshot (optional) ==="
echo "To export Prometheus data, run:"
echo "  kubectl --context is-chaos-${ENV_NAME} -n monitoring exec -it prometheus-prometheus-kube-prometheus-prometheus-0 -- promtool tsdb snapshot /prometheus"
echo "  kubectl --context is-chaos-${ENV_NAME} -n monitoring cp prometheus-prometheus-kube-prometheus-prometheus-0:/prometheus/snapshots/ $DATA_DIR/prometheus-snapshot/"
echo ""

echo "=== Export complete ==="
echo "Data uploaded to: $DEST"
