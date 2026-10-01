#!/usr/bin/env bash
################################################################################
# Component 1 batch runner (run-experiment.py --mode benchmark). Overhead runs
# use run-overhead.sh instead.
#
# Full design: 2 tools x 12 scenarios x 30 reps = 720 runs. The study split it
# across two identical clusters: bench-a ran --tool chaos-mesh and bench-b ran
# --tool litmus, 360 runs each.
#
# Each rep is preceded by reset-app-state.sh unless CHAOS_RESET_STATE=0.
# Resume: a (tool, scenario, rep) is skipped when
# $CHAOS_DATA_DIR/{tool}/{scenario}/run-{rep}.json already exists.
#
# Env: CHAOS_DATA_DIR (required), CHAOS_SLOT (default 0, overridden by
# --slot), CHAOS_RESET_STATE (default 1).
#
# Usage:
#   ./scripts/run-all-experiments.sh [--tool chaos-mesh|litmus|all] [--reps N] [--start-rep N] [--scenarios p1,p2,...] [--slot N]
#
# Examples:
#   bench-a$ ./scripts/run-all-experiments.sh --tool chaos-mesh --reps 30
#   bench-b$ ./scripts/run-all-experiments.sh --tool litmus --reps 30
#
#   Resume from rep 14. Existing outputs inside the range are still skipped,
#   --start-rep only avoids checking the earlier reps:
#   bench-a$ ./scripts/run-all-experiments.sh --tool chaos-mesh --reps 30 --start-rep 14
#
#   Both tools on one cluster:
#   $ ./scripts/run-all-experiments.sh --tool all --reps 30
#
# Slot parallelism (see scripts/SLOT_PARALLELISM.md): up to 3 invocations can
# run on one cluster, one per slot.
#   --scenarios  restricts the invocation to a comma-separated list of scenario
#       IDs. The default is all 12 scenarios.
#   --slot N  exports CHAOS_SLOT=N for run-experiment.py and
#       reset-app-state.sh. Slots other than 0 prefix every progress.log line
#       with "[slot N]" so concurrent invocations sharing one log stay
#       distinguishable.
#
#   Output paths have no slot component, so concurrent slots sharing one
#   CHAOS_DATA_DIR must use disjoint --scenarios sets. The script does not
#   check this.
#
#   Example (3 concurrent slots on one cluster, disjoint scenario sets):
#   slot0$ CHAOS_SLOT=0 ./scripts/run-all-experiments.sh --tool chaos-mesh --scenarios p1,p2,p3,n1 --reps 30
#   slot1$ ./scripts/run-all-experiments.sh --tool chaos-mesh --scenarios n2,n3,n4,n5 --slot 1 --reps 30
#   slot2$ ./scripts/run-all-experiments.sh --tool chaos-mesh --scenarios r1,r2,a1,a2 --slot 2 --reps 30
################################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

export AWS_PROFILE="${AWS_PROFILE:-default}"

if [[ -z "${CHAOS_DATA_DIR:-}" ]]; then
    echo "ERROR: CHAOS_DATA_DIR is not set. No default -- a missing/wrong default here" >&2
    echo "silently misdirected standalone runs before (see chaoslib.py's DATA_DIR check)." >&2
    echo "Set it explicitly, e.g. export CHAOS_DATA_DIR=\"\$(pwd)/data-v2/bench-a\"." >&2
    exit 1
fi
DATA_DIR="${CHAOS_DATA_DIR}"
PROGRESS_LOG="${DATA_DIR}/progress.log"

ALL_SCENARIOS=("p1" "p2" "p3" "n1" "n2" "n3" "n4" "n5" "r1" "r2" "a1" "a2")
SCENARIOS=("${ALL_SCENARIOS[@]}")

TOOL_FILTER="all"
REPS=30
START_REP=1
SCENARIOS_ARG=""
SLOT="${CHAOS_SLOT:-0}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tool)
            TOOL_FILTER="${2:-}"
            shift 2
            ;;
        --reps)
            REPS="${2:-}"
            shift 2
            ;;
        --start-rep)
            START_REP="${2:-}"
            shift 2
            ;;
        --scenarios)
            SCENARIOS_ARG="${2:-}"
            shift 2
            ;;
        --slot)
            SLOT="${2:-}"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [--tool chaos-mesh|litmus|all] [--reps N] [--start-rep N] [--scenarios p1,p2,...] [--slot N]"
            exit 0
            ;;
        *)
            echo "ERROR: Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

# --scenarios replaces the default list of all 12 scenarios.
if [[ -n "${SCENARIOS_ARG}" ]]; then
    IFS=',' read -ra SCENARIOS <<< "${SCENARIOS_ARG}"
    for s in "${SCENARIOS[@]}"; do
        valid=0
        for known in "${ALL_SCENARIOS[@]}"; do
            [[ "$s" == "$known" ]] && valid=1 && break
        done
        if [[ "$valid" -ne 1 ]]; then
            echo "ERROR: --scenarios contains unknown scenario '$s'. Valid: ${ALL_SCENARIOS[*]}" >&2
            exit 1
        fi
    done
fi

# Exported for run-experiment.py (read by chaoslib) and reset-app-state.sh.
# Slot 0 maps to the default namespace social-network.
export CHAOS_SLOT="${SLOT}"

# Progress-log prefix for slots other than 0. experiment-watchdog.py parses
# lines with and without it.
LOG_PREFIX=""
if [[ -n "${SLOT}" && "${SLOT}" != "0" ]]; then
    LOG_PREFIX="[slot ${SLOT}] "
fi

case "${TOOL_FILTER}" in
    chaos-mesh) TOOLS=("chaos-mesh") ;;
    litmus)     TOOLS=("litmus") ;;
    all)        TOOLS=("chaos-mesh" "litmus") ;;
    *)
        echo "ERROR: --tool must be chaos-mesh, litmus, or all (got '${TOOL_FILTER}')" >&2
        exit 1
        ;;
esac

if ! [[ "${REPS}" =~ ^[0-9]+$ ]] || [[ "${REPS}" -lt 1 ]]; then
    echo "ERROR: --reps must be a positive integer (got '${REPS}')" >&2
    exit 1
fi
if ! [[ "${START_REP}" =~ ^[0-9]+$ ]] || [[ "${START_REP}" -lt 1 ]] || [[ "${START_REP}" -gt "${REPS}" ]]; then
    echo "ERROR: --start-rep must be between 1 and --reps (${REPS}), got '${START_REP}'" >&2
    exit 1
fi
if ! [[ "${SLOT}" =~ ^[0-9]+$ ]]; then
    echo "ERROR: --slot must be a non-negative integer (got '${SLOT}')" >&2
    exit 1
fi

TOTAL=$(( ${#TOOLS[@]} * ${#SCENARIOS[@]} * (REPS - START_REP + 1) ))
CURRENT=0
SKIPPED=0
FAILED=0

mkdir -p "${DATA_DIR}"

echo "${LOG_PREFIX}================================================================================" | tee -a "${PROGRESS_LOG}"
echo "${LOG_PREFIX}  Chaos Benchmark - Batch Experiment Runner" | tee -a "${PROGRESS_LOG}"
echo "${LOG_PREFIX}  Tools: ${TOOLS[*]} | Scenarios: ${#SCENARIOS[@]} | Reps: ${START_REP}-${REPS}" | tee -a "${PROGRESS_LOG}"
echo "${LOG_PREFIX}  Total experiments this invocation: ${TOTAL}" | tee -a "${PROGRESS_LOG}"
# Log the reset setting, so a run with resets disabled is visible in
# progress.log (see analysis/PREREGISTRATION.md, Component 1 amendment item 3).
if [[ "${CHAOS_RESET_STATE:-1}" != "1" ]]; then
    echo "${LOG_PREFIX}  WARNING: CHAOS_RESET_STATE=${CHAOS_RESET_STATE} -- per-rep state reset is DISABLED for this invocation" | tee -a "${PROGRESS_LOG}"
else
    echo "${LOG_PREFIX}  CHAOS_RESET_STATE=1 (default): per-rep state reset ENABLED" | tee -a "${PROGRESS_LOG}"
fi
echo "${LOG_PREFIX}  Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a "${PROGRESS_LOG}"
echo "${LOG_PREFIX}================================================================================" | tee -a "${PROGRESS_LOG}"

for tool in "${TOOLS[@]}"; do
    for scenario in "${SCENARIOS[@]}"; do
        for run in $(seq "${START_REP}" "${REPS}"); do
            CURRENT=$((CURRENT + 1))
            OUTPUT_FILE="${DATA_DIR}/${tool}/${scenario}/run-${run}.json"

            # Resume support: skip if output already exists
            if [[ -f "${OUTPUT_FILE}" ]]; then
                echo "${LOG_PREFIX}[${CURRENT}/${TOTAL}] SKIP ${tool} / ${scenario} / run ${run} (exists)" | tee -a "${PROGRESS_LOG}"
                SKIPPED=$((SKIPPED + 1))
                continue
            fi

            echo "" | tee -a "${PROGRESS_LOG}"
            echo "${LOG_PREFIX}[${CURRENT}/${TOTAL}] RUN  ${tool} / ${scenario} / run ${run}" | tee -a "${PROGRESS_LOG}"
            echo "${LOG_PREFIX}  Time: $(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a "${PROGRESS_LOG}"

            # Reset app state before every rep (CHAOS_RESET_STATE=0 disables
            # it). CHAOS_SLOT is exported above, so the reset targets this
            # slot's namespace.
            if [[ "${CHAOS_RESET_STATE:-1}" == "1" ]]; then
                echo "${LOG_PREFIX}  [reset] resetting app state..." | tee -a "${PROGRESS_LOG}"
                if ! "${SCRIPT_DIR}/reset-app-state.sh" >> "${PROGRESS_LOG}" 2>&1; then
                    echo "${LOG_PREFIX}  FAIL: ${tool} / ${scenario} / run ${run} (state reset failed)" | tee -a "${PROGRESS_LOG}"
                    FAILED=$((FAILED + 1))
                    continue
                fi
            fi

            if python3 "${SCRIPT_DIR}/run-experiment.py" \
                --mode benchmark \
                --tool "${tool}" \
                --scenario "${scenario}" \
                --run "${run}" 2>&1 | tee -a "${PROGRESS_LOG}"; then
                echo "${LOG_PREFIX}  PASS: ${tool} / ${scenario} / run ${run}" | tee -a "${PROGRESS_LOG}"
            else
                echo "${LOG_PREFIX}  FAIL: ${tool} / ${scenario} / run ${run}" | tee -a "${PROGRESS_LOG}"
                FAILED=$((FAILED + 1))
                # Continue to next experiment (don't abort the batch)
            fi
        done
    done
done

COMPLETED=$((CURRENT - SKIPPED - FAILED))

echo "" | tee -a "${PROGRESS_LOG}"
echo "${LOG_PREFIX}================================================================================" | tee -a "${PROGRESS_LOG}"
echo "${LOG_PREFIX}  Batch Complete" | tee -a "${PROGRESS_LOG}"
echo "${LOG_PREFIX}  Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a "${PROGRESS_LOG}"
echo "${LOG_PREFIX}  Total:     ${TOTAL}" | tee -a "${PROGRESS_LOG}"
echo "${LOG_PREFIX}  Completed: ${COMPLETED}" | tee -a "${PROGRESS_LOG}"
echo "${LOG_PREFIX}  Skipped:   ${SKIPPED}" | tee -a "${PROGRESS_LOG}"
echo "${LOG_PREFIX}  Failed:    ${FAILED}" | tee -a "${PROGRESS_LOG}"
echo "${LOG_PREFIX}================================================================================" | tee -a "${PROGRESS_LOG}"

if [[ ${FAILED} -gt 0 ]]; then
    echo "WARNING: ${FAILED} experiment(s) failed. Check ${PROGRESS_LOG} for details."
    exit 1
fi
