#!/usr/bin/env bash
################################################################################
# Build the wrk2 load-generator image and push it to ECR as
# <account>.dkr.ecr.<region>.amazonaws.com/chaos-benchmark/wrk2:latest.
# Export that repository path as CHAOS_ECR_REPO for the experiment runners.
# Env: AWS_PROFILE (default "default"), AWS_REGION (default ca-central-1).
# Requires aws, docker and git.
# Usage: ./scripts/build-wrk2-image.sh
################################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

export AWS_PROFILE="${AWS_PROFILE:-default}"
export DOCKER_BUILDKIT=1
AWS_REGION="${AWS_REGION:-ca-central-1}"
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ECR_REPO="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/chaos-benchmark/wrk2"

echo "=== Building wrk2 Docker image ==="
echo "ECR Repository: ${ECR_REPO}"

# Ensure wrk2 submodule and its deps (luajit) are initialized
echo "--- Ensuring git submodules are initialized..."
git -C "${PROJECT_ROOT}" submodule update --init --recursive

# Create ECR repository if it doesn't exist
echo "--- Ensuring ECR repository exists..."
aws ecr describe-repositories \
    --repository-names chaos-benchmark/wrk2 \
    --region "${AWS_REGION}" 2>/dev/null || \
aws ecr create-repository \
    --repository-name chaos-benchmark/wrk2 \
    --region "${AWS_REGION}" \
    --image-scanning-configuration scanOnPush=true

# Authenticate Docker with ECR
echo "--- Authenticating Docker with ECR..."
aws ecr get-login-password --region "${AWS_REGION}" | \
    docker login --username AWS --password-stdin \
    "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

# Build from a temporary context that holds only the Dockerfile, the Lua
# workload script and the wrk2 source, so the whole DeathStarBench submodule
# is not sent to Docker.
BUILD_DIR=$(mktemp -d)
trap "rm -rf ${BUILD_DIR}" EXIT

echo "--- Preparing build context..."
cp "${PROJECT_ROOT}/load-generator/Dockerfile" "${BUILD_DIR}/"
cp "${PROJECT_ROOT}/load-generator/mixed-workload.lua" "${BUILD_DIR}/"
cp -r "${PROJECT_ROOT}/DeathStarBench/wrk2" "${BUILD_DIR}/wrk2"

echo "--- Building Docker image (platform: linux/amd64)..."
docker build \
    --platform linux/amd64 \
    -t "${ECR_REPO}:latest" \
    "${BUILD_DIR}"

echo "--- Pushing to ECR..."
docker push "${ECR_REPO}:latest"

echo "=== wrk2 image built and pushed successfully ==="
echo "Image: ${ECR_REPO}:latest"
