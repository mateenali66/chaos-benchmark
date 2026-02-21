#!/usr/bin/env bash
################################################################################
# Build and push wrk2 Docker image to ECR
# Usage: ./scripts/build-wrk2-image.sh
################################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

export AWS_PROFILE=personal
export DOCKER_BUILDKIT=1
AWS_REGION="us-east-1"
AWS_ACCOUNT_ID="886604922358"
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

# Build context: load-generator/ dir with wrk2 source copied in
# We use a temporary build context to avoid sending the entire DSB submodule
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
