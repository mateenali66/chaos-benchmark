#!/usr/bin/env bash
# Environment for the experiment runners. Source it, do not execute it:
#   source scripts/campaign-env.sh
# Sets AWS_REGION=ca-central-1, CHAOS_ECR_REPO (this account's wrk2 image,
# pushed by build-wrk2-image.sh) and CHAOS_LOAD_RPS. The clusters are
# is-chaos-bench-a, is-chaos-bench-b and is-chaos-ml. CHAOS_DATA_DIR is not
# set here and must be exported separately.

export AWS_PROFILE="${AWS_PROFILE:-default}"
export AWS_REGION="ca-central-1"
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
export CHAOS_ECR_REPO="${CHAOS_ECR_REPO:-${AWS_ACCOUNT_ID}.dkr.ecr.ca-central-1.amazonaws.com/chaos-benchmark/wrk2}"

# Offered load in requests per second, the same for every run (see
# chaoslib.WRK_RATE and analysis/PREREGISTRATION.md, Component 1 amendment
# item 4).
export CHAOS_LOAD_RPS="120"
