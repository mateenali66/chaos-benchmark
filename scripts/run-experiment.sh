#!/usr/bin/env bash
################################################################################
# Single-run wrapper: sets AWS_PROFILE (default "default") and passes all
# arguments to run-experiment.py. CHAOS_DATA_DIR must be set.
# Usage: ./scripts/run-experiment.sh --tool chaos-mesh --scenario p1 --run 1
################################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export AWS_PROFILE="${AWS_PROFILE:-default}"

exec python3 "${SCRIPT_DIR}/run-experiment.py" "$@"
