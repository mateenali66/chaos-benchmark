#!/usr/bin/env bash
# Source this before any experiment command of the JSS revision campaign:
#   source scripts/campaign-env.sh
# Account 759890811490 (staging), ca-central-1, clusters is-chaos-{bench-a,bench-b,ml}.

export AWS_PROFILE="${AWS_PROFILE:-is-staging-mfa}"
export AWS_REGION="ca-central-1"
export CHAOS_ECR_REPO="759890811490.dkr.ecr.ca-central-1.amazonaws.com/chaos-benchmark/wrk2"

# Offered load: fixed for the whole revision campaign (see chaoslib.WRK_RATE)
export CHAOS_LOAD_RPS="120"
