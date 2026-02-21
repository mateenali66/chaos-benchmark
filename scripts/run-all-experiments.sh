#!/usr/bin/env bash
################################################################################
# Batch Experiment Runner
# Runs all experiments: 2 tools x 12 scenarios x 5 runs = 120 total
# Supports resume: skips runs where output JSON already exists
# Usage: ./scripts/run-all-experiments.sh
################################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

export AWS_PROFILE=personal

DATA_DIR="${PROJECT_ROOT}/data"
PROGRESS_LOG="${DATA_DIR}/progress.log"

TOOLS=("chaos-mesh" "litmus")
SCENARIOS=("p1" "p2" "p3" "n1" "n2" "n3" "n4" "n5" "r1" "r2" "a1" "a2")
RUNS=5

TOTAL=$((${#TOOLS[@]} * ${#SCENARIOS[@]} * RUNS))
CURRENT=0
SKIPPED=0
FAILED=0

mkdir -p "${DATA_DIR}"

echo "================================================================================" | tee -a "${PROGRESS_LOG}"
echo "  Chaos Benchmark - Batch Experiment Runner" | tee -a "${PROGRESS_LOG}"
echo "  Total experiments: ${TOTAL} (${#TOOLS[@]} tools x ${#SCENARIOS[@]} scenarios x ${RUNS} runs)" | tee -a "${PROGRESS_LOG}"
echo "  Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a "${PROGRESS_LOG}"
echo "================================================================================" | tee -a "${PROGRESS_LOG}"

for tool in "${TOOLS[@]}"; do
    for scenario in "${SCENARIOS[@]}"; do
        for run in $(seq 1 ${RUNS}); do
            CURRENT=$((CURRENT + 1))
            OUTPUT_FILE="${DATA_DIR}/${tool}/${scenario}/run-${run}.json"

            # Resume support: skip if output already exists
            if [[ -f "${OUTPUT_FILE}" ]]; then
                echo "[${CURRENT}/${TOTAL}] SKIP ${tool} / ${scenario} / run ${run} (exists)" | tee -a "${PROGRESS_LOG}"
                SKIPPED=$((SKIPPED + 1))
                continue
            fi

            echo "" | tee -a "${PROGRESS_LOG}"
            echo "[${CURRENT}/${TOTAL}] RUN  ${tool} / ${scenario} / run ${run}" | tee -a "${PROGRESS_LOG}"
            echo "  Time: $(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a "${PROGRESS_LOG}"

            if python3 "${SCRIPT_DIR}/run-experiment.py" \
                --tool "${tool}" \
                --scenario "${scenario}" \
                --run "${run}" 2>&1 | tee -a "${PROGRESS_LOG}"; then
                echo "  PASS: ${tool} / ${scenario} / run ${run}" | tee -a "${PROGRESS_LOG}"
            else
                echo "  FAIL: ${tool} / ${scenario} / run ${run}" | tee -a "${PROGRESS_LOG}"
                FAILED=$((FAILED + 1))
                # Continue to next experiment (don't abort the batch)
            fi
        done
    done
done

COMPLETED=$((CURRENT - SKIPPED - FAILED))

echo "" | tee -a "${PROGRESS_LOG}"
echo "================================================================================" | tee -a "${PROGRESS_LOG}"
echo "  Batch Complete" | tee -a "${PROGRESS_LOG}"
echo "  Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a "${PROGRESS_LOG}"
echo "  Total:     ${TOTAL}" | tee -a "${PROGRESS_LOG}"
echo "  Completed: ${COMPLETED}" | tee -a "${PROGRESS_LOG}"
echo "  Skipped:   ${SKIPPED}" | tee -a "${PROGRESS_LOG}"
echo "  Failed:    ${FAILED}" | tee -a "${PROGRESS_LOG}"
echo "================================================================================" | tee -a "${PROGRESS_LOG}"

if [[ ${FAILED} -gt 0 ]]; then
    echo "WARNING: ${FAILED} experiment(s) failed. Check ${PROGRESS_LOG} for details."
    exit 1
fi
