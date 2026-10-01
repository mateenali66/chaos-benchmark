#!/usr/bin/env bash
################################################################################
# Continuous backup of one cluster's data directory to the artifacts bucket.
#
# Every interval_s seconds (default 600) syncs CHAOS_DATA_DIR (default
# data-v2/<env>) to s3://<bucket>/<env>/data-v2/. The bucket is
# CHAOS_S3_BUCKET, or is-chaos-artifacts-<account_id> if unset. Each pass
# appends a status line to <data dir>/watchdogs/s3-sync-status.jsonl. A failed
# sync also writes an .ALERT sentinel next to it, like the other watchdogs,
# and the loop keeps running so a transient auth or network error does not
# stop the backup.
#
# Env: AWS_PROFILE (default "default").
# Usage: ./scripts/watchdogs/s3-sync-loop.sh <bench-a|bench-b|ml> [interval_s]
################################################################################
set -uo pipefail

ENV_NAME="${1:?usage: s3-sync-loop.sh <bench-a|bench-b|ml> [interval_s]}"
INTERVAL="${2:-600}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

export AWS_PROFILE="${AWS_PROFILE:-default}"
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${CHAOS_S3_BUCKET:-is-chaos-artifacts-${AWS_ACCOUNT_ID}}"
DATA_DIR="${CHAOS_DATA_DIR:-${PROJECT_DIR}/data-v2/${ENV_NAME}}"
STATUS_FILE="${DATA_DIR}/watchdogs/s3-sync-status.jsonl"
mkdir -p "$(dirname "$STATUS_FILE")"

echo "s3-sync-loop: ${DATA_DIR} -> s3://${BUCKET}/${ENV_NAME}/data-v2/ every ${INTERVAL}s"

while true; do
    TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if OUT=$(aws s3 sync "$DATA_DIR" "s3://${BUCKET}/${ENV_NAME}/data-v2/" \
        --region ca-central-1 --only-show-errors --no-progress 2>&1); then
        COUNT=$(find "$DATA_DIR" -type f | wc -l | tr -d ' ')
        echo "{\"ts\":\"$TS\",\"watchdog\":\"s3-sync\",\"level\":\"OK\",\"local_files\":$COUNT}" >> "$STATUS_FILE"
        rm -f "${STATUS_FILE}.ALERT"
    else
        MSG=$(echo "$OUT" | head -1 | tr '"' "'")
        echo "{\"ts\":\"$TS\",\"watchdog\":\"s3-sync\",\"level\":\"ALERT\",\"error\":\"$MSG\"}" >> "$STATUS_FILE"
        touch "${STATUS_FILE}.ALERT"
    fi
    sleep "$INTERVAL"
done
