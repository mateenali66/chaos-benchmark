#!/usr/bin/env bash
################################################################################
# Continuous S3 backup loop (February-campaign lesson: local-only results are
# one laptop failure away from a lost campaign).
#
# Syncs a cluster's data root to the versioned artifacts bucket every
# INTERVAL seconds. Sync failures write an .ALERT sentinel (same convention as
# the other watchdogs) but the loop keeps running: transient auth/network
# errors must not kill the backup.
#
# Usage: ./scripts/watchdogs/s3-sync-loop.sh <bench-a|bench-b|ml> [interval_s]
################################################################################
set -uo pipefail

ENV_NAME="${1:?usage: s3-sync-loop.sh <bench-a|bench-b|ml> [interval_s]}"
INTERVAL="${2:-600}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

export AWS_PROFILE="${AWS_PROFILE:-is-staging-mfa}"
BUCKET="is-chaos-artifacts-759890811490"
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
