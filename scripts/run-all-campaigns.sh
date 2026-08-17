#!/usr/bin/env bash
################################################################################
# Component 3 batch driver: 5 arms (random, coverage, llm-claude, llm-llama,
# llm-mistral) x 10 campaigns = 50 campaigns, 500 injections total, on the
# dedicated is-chaos-ml cluster (analysis/PREREGISTRATION.md's Component 3).
# --tool litmus for every campaign (the LLM arms' "select from a ChaosCenter
# menu" framing, ../jss/ML_ARM_DESIGN.md, is LitmusChaos-specific; chaos-mesh
# has no MCP/AI story per that doc's verified fact #5).
#
# 3-slot parallelism: the 50 (arm, campaign) pairs are assigned to slots by
# flat index % 3 (see the loop below), giving each slot a mix of all 5 arms
# rather than one slot serially draining a single arm. Requires:
#   - DSB deployed + node-pinned in social-network(-1/-2) (deploy-dsb.sh)
#   - ChaosExperiment CRDs + Litmus RBAC per slot (post-deploy.sh)
#   - Node labels chaos-slot=0/1/2 (kubectl label, done once manually)
#   - run-campaign.py's manifest namespace substitution + chaoslib's
#     Prometheus port-forward collision guard (both fixed 2026-08-16
#     specifically for this launch -- see jss/REVISION_PLAN.md)
#
# Usage (one terminal/process per slot, matching run-all-experiments.sh's
# slot convention):
#   CHAOS_SLOT=0 ./scripts/run-all-campaigns.sh
#   CHAOS_SLOT=1 ./scripts/run-all-campaigns.sh
#   CHAOS_SLOT=2 ./scripts/run-all-campaigns.sh
#
# Resume: run-campaign.py itself resumes each (arm, campaign) internally
# (skips injections whose injection-N.json already exists) and this driver
# skips a campaign entirely once campaign-summary.json reports 10/10
# injections, so re-running any slot after an interruption is always safe.
################################################################################
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

export AWS_PROFILE="${AWS_PROFILE:-is-staging-mfa}"
export CHAOS_SLOT="${CHAOS_SLOT:-0}"
export CHAOS_DATA_DIR="${CHAOS_DATA_DIR:-${PROJECT_ROOT}/data-v2/ml}"
# chaoslib.ECR_REPO defaults to a stale cross-account (886604922358, the
# "personal" AWS account) image reference -- 403 Forbidden under
# is-staging-mfa's node IAM role (found live 2026-08-16 debugging the
# smoke-test campaign's zero-throughput wrk2 runs). The real per-account
# image already exists (pushed by build-wrk2-image.sh); must be selected
# explicitly, matching chaoslib.py's own documented override convention.
export CHAOS_ECR_REPO="${CHAOS_ECR_REPO:-759890811490.dkr.ecr.ca-central-1.amazonaws.com/chaos-benchmark/wrk2}"

CAMPAIGN_K=10
ARMS=("random" "coverage" "llm-claude" "llm-llama" "llm-mistral")

LOG_PREFIX=""
if [[ "${CHAOS_SLOT}" != "0" ]]; then
    LOG_PREFIX="[slot ${CHAOS_SLOT}] "
fi

PROGRESS_LOG="${CHAOS_DATA_DIR}/campaigns-progress.log"
mkdir -p "${CHAOS_DATA_DIR}"

log() {
    echo "${LOG_PREFIX}$1" | tee -a "${PROGRESS_LOG}"
}

log "=== Component 3 campaign driver starting on slot ${CHAOS_SLOT} ==="

TOTAL_ASSIGNED=0
TOTAL_DONE=0
TOTAL_FAILED=0

for arm_idx in "${!ARMS[@]}"; do
    arm="${ARMS[$arm_idx]}"
    for campaign in $(seq 1 10); do
        idx=$(( arm_idx * 10 + (campaign - 1) ))
        slot_for_idx=$(( idx % 3 ))
        if [[ "${slot_for_idx}" != "${CHAOS_SLOT}" ]]; then
            continue
        fi
        TOTAL_ASSIGNED=$(( TOTAL_ASSIGNED + 1 ))

        SUMMARY_FILE="${CHAOS_DATA_DIR}/campaigns/${arm}/campaign-${campaign}/campaign-summary.json"
        if [[ -f "${SUMMARY_FILE}" ]]; then
            DONE_COUNT=$(python3 -c "
import json
with open('${SUMMARY_FILE}') as f:
    d = json.load(f)
print(d.get('injections_completed', 0))
" 2>/dev/null || echo 0)
            if [[ "${DONE_COUNT}" == "${CAMPAIGN_K}" ]]; then
                log "[${idx}/49] SKIP ${arm}/campaign-${campaign} (already ${CAMPAIGN_K}/${CAMPAIGN_K})"
                TOTAL_DONE=$(( TOTAL_DONE + 1 ))
                continue
            fi
        fi

        SEED_ARG=()
        if [[ "${arm}" == "random" ]]; then
            SEED_ARG=(--seed "${campaign}")
        fi

        log "[${idx}/49] RUN ${arm}/campaign-${campaign}"
        if python3 "${SCRIPT_DIR}/run-campaign.py" --tool litmus --strategy "${arm}" \
                --campaign "${campaign}" "${SEED_ARG[@]}" \
                >> "${CHAOS_DATA_DIR}/campaigns-output.log" 2>&1; then
            log "[${idx}/49] PASS ${arm}/campaign-${campaign}"
            TOTAL_DONE=$(( TOTAL_DONE + 1 ))
        else
            log "[${idx}/49] FAIL ${arm}/campaign-${campaign} (rc=$?, see campaigns-output.log)"
            TOTAL_FAILED=$(( TOTAL_FAILED + 1 ))
        fi
    done
done

log "=== Slot ${CHAOS_SLOT} Batch Complete: ${TOTAL_DONE}/${TOTAL_ASSIGNED} done, ${TOTAL_FAILED} failed ==="
