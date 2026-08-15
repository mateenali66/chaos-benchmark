#!/usr/bin/env bash
# Source this before any experiment command of the JSS revision campaign:
#   source scripts/campaign-env.sh
# Account ACCOUNT_ID (staging), ca-central-1, clusters is-chaos-{bench-a,bench-b,ml}.

export AWS_PROFILE="${AWS_PROFILE:-default}"
export AWS_REGION="ca-central-1"
export CHAOS_ECR_REPO="ACCOUNT_ID.dkr.ecr.ca-central-1.amazonaws.com/chaos-benchmark/wrk2"

# Offered load: fixed for the whole revision campaign (see chaoslib.WRK_RATE)
export CHAOS_LOAD_RPS="120"
