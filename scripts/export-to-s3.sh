#!/usr/bin/env bash
set -euo pipefail

################################################################################
# Export Experiment Data to S3
# Syncs local data/ directory to the S3 bucket
################################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DATA_DIR="$PROJECT_DIR/data"
AWS_PROFILE="${AWS_PROFILE:-personal}"

# Get S3 bucket from Terraform output
BUCKET=$(cd "$PROJECT_DIR/terraform" && terraform output -raw s3_bucket 2>/dev/null)

if [ -z "$BUCKET" ]; then
  echo "ERROR: Could not get S3 bucket name from Terraform output."
  echo "Make sure terraform has been applied: cd terraform && terraform output s3_bucket"
  exit 1
fi

TIMESTAMP=$(date +%Y%m%d-%H%M%S)

echo "=== Exporting experiment data to S3 ==="
echo "Source: $DATA_DIR"
echo "Destination: s3://$BUCKET/"
echo "Profile: $AWS_PROFILE"
echo ""

# Sync data directory
aws s3 sync "$DATA_DIR" "s3://$BUCKET/data/$TIMESTAMP/" \
  --profile "$AWS_PROFILE" \
  --exclude ".gitkeep"

echo ""
echo "=== Prometheus snapshot (optional) ==="
echo "To export Prometheus data, run:"
echo "  kubectl -n monitoring exec -it prometheus-prometheus-kube-prometheus-prometheus-0 -- promtool tsdb snapshot /prometheus"
echo "  kubectl -n monitoring cp prometheus-prometheus-kube-prometheus-prometheus-0:/prometheus/snapshots/ $DATA_DIR/prometheus-snapshot/"
echo ""

echo "=== Export complete ==="
echo "Data uploaded to: s3://$BUCKET/data/$TIMESTAMP/"
