#!/usr/bin/env bash
################################################################################
# Experiment Runner Wrapper
# Sets environment and delegates to Python orchestrator
# Usage: ./scripts/run-experiment.sh --tool chaos-mesh --scenario p1 --run 1
################################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export AWS_PROFILE=personal

exec python3 "${SCRIPT_DIR}/run-experiment.py" "$@"
