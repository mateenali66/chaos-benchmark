#!/usr/bin/env bash
# Source this before any experiment command of the JSS revision campaign:
#   source scripts/campaign-env.sh
# Staging account, ca-central-1, clusters is-chaos-{bench-a,bench-b,ml}.

export AWS_PROFILE="${AWS_PROFILE:-is-staging-mfa}"
export AWS_REGION="ca-central-1"
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
export CHAOS_ECR_REPO="${CHAOS_ECR_REPO:-${AWS_ACCOUNT_ID}.dkr.ecr.ca-central-1.amazonaws.com/chaos-benchmark/wrk2}"

# Offered load: fixed for the whole revision campaign (see chaoslib.WRK_RATE)
export CHAOS_LOAD_RPS="120"
