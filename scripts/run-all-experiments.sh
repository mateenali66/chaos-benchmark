#!/usr/bin/env bash
################################################################################
# Batch Experiment Runner (--mode benchmark, i.e. the original 4-phase
# protocol; overhead-isolation runs are driven by run-overhead.sh instead)
#
# Full design: up to 2 tools x 12 scenarios x REPS runs (REPS default 30 ->
# 720 total across both tools). This is split across two identical clusters
# for the re-run at scale: bench-a runs --tool chaos-mesh, bench-b runs
# --tool litmus, each producing 360 of the 720 runs.
#
# Resume support: skips (tool, scenario, rep) triples whose output JSON
# already exists at data/{tool}/{scenario}/run-{rep}.json. This is safe to
# run on a cluster that only has one tool's data -- e.g. re-running this on
# bench-a with --tool chaos-mesh only ever looks at chaos-mesh output files,
# so it never notices (and never needs) litmus's files from bench-b.
#
# Usage:
#   ./scripts/run-all-experiments.sh [--tool chaos-mesh|litmus|all] [--reps N] [--start-rep N] [--scenarios p1,p2,...] [--slot N]
#
# Examples (two-cluster layout):
#   bench-a$ ./scripts/run-all-experiments.sh --tool chaos-mesh --reps 30
#   bench-b$ ./scripts/run-all-experiments.sh --tool litmus --reps 30
#
#   Resume bench-a after a crash partway through rep 14 of 30 (skips nothing
#   extra beyond what --start-rep says to skip; existing-file resume still
#   applies within the range too, so this is just an optimization to avoid
#   re-checking 13 x 12 = 156 files you already know are done):
#   bench-a$ ./scripts/run-all-experiments.sh --tool chaos-mesh --reps 30 --start-rep 14
#
#   Single-cluster/local run of everything (original layout, both tools):
#   $ ./scripts/run-all-experiments.sh --tool all --reps 30
#
# Slot parallelism (3 concurrent invocations on ONE cluster, one per slot):
#   --scenarios p1,p2,p3,n1  restricts this invocation to an explicit,
#       comma-separated scenario ID allowlist. Omitted (default) = all 12
#       scenarios, unchanged from before slot support existed.
#   --slot N  sets/exports CHAOS_SLOT=N for run-experiment.py and
#       reset-app-state.sh, and prefixes every progress-log line with
#       "[slot N]" so a shared progress.log from concurrent slot invocations
#       stays distinguishable. Slot 0 (or --slot omitted) adds no prefix --
#       byte-for-byte the original log format, so experiment-watchdog.py's
#       parsing of an unset-CHAOS_SLOT campaign's log is unaffected.
#
#   IMPORTANT / caller responsibility: CHAOS_DATA_DIR should normally be the
#   SAME shared directory across a cluster's concurrent slot invocations.
#   Output paths are data/{tool}/{scenario}/run-{rep}.json -- scenario+run+
#   tool alone identifies a file, with no slot component, because a run's
#   scenario+tool+rep triple is assumed unique per invocation. This only
#   holds if the --scenarios sets passed to concurrent slots on one cluster
#   are DISJOINT (e.g. slot 0 = p1,p2,p3,n1 / slot 1 = n2,n3,n4,n5 / slot 2 =
#   r1,r2,a1,a2). Two slots racing on the SAME scenario+tool+rep would race
#   on the same output file. Enforcing disjoint scenario sets across
#   concurrent slots is the caller's responsibility, not this script's --
#   see scripts/SLOT_PARALLELISM.md for a worked 3-way split example.
#   Concurrent appends to the same progress.log are fine (each `tee -a` line
#   write is small enough to not interleave mid-line in practice).
#
#   Example (3 concurrent slots on one cluster, disjoint scenario sets):
#   slot0$ CHAOS_SLOT=0 ./scripts/run-all-experiments.sh --tool chaos-mesh --scenarios p1,p2,p3,n1 --reps 30
#   slot1$ ./scripts/run-all-experiments.sh --tool chaos-mesh --scenarios n2,n3,n4,n5 --slot 1 --reps 30
#   slot2$ ./scripts/run-all-experiments.sh --tool chaos-mesh --scenarios r1,r2,a1,a2 --slot 2 --reps 30
################################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

export AWS_PROFILE="${AWS_PROFILE:-is-staging-mfa}"

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

# --scenarios overrides the default full 12-scenario list. Omitted =
# unchanged behavior (all 12, in the original order).
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

# --slot sets/exports CHAOS_SLOT for run-experiment.py (chaoslib picks it up
# via os.environ) and reset-app-state.sh. Slot 0/unset -> exported as "0",
# which chaoslib.namespace_for_slot() maps back to the original default
# namespace, so this is a no-op for the currently-running default campaign.
export CHAOS_SLOT="${SLOT}"

# Log-line prefix for concurrent slot invocations sharing one progress.log.
# Slot 0/unset -> empty prefix, i.e. byte-for-byte the original log format
# (experiment-watchdog.py parses this log and must not see a format change
# on the unset-CHAOS_SLOT path).
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

# CHAOS_DATA_DIR assumption: this should normally be the SAME shared
# directory across a cluster's concurrent slot invocations (see the header
# comment above). Output paths never include slot, only tool/scenario/run,
# so disjoint --scenarios sets across concurrent slots is what keeps this
# safe -- that invariant is the caller's responsibility, not enforced here.
mkdir -p "${DATA_DIR}"

echo "${LOG_PREFIX}================================================================================" | tee -a "${PROGRESS_LOG}"
echo "${LOG_PREFIX}  Chaos Benchmark - Batch Experiment Runner" | tee -a "${PROGRESS_LOG}"
echo "${LOG_PREFIX}  Tools: ${TOOLS[*]} | Scenarios: ${#SCENARIOS[@]} | Reps: ${START_REP}-${REPS}" | tee -a "${PROGRESS_LOG}"
echo "${LOG_PREFIX}  Total experiments this invocation: ${TOTAL}" | tee -a "${PROGRESS_LOG}"
# Explicit, logged acknowledgment of the resolved value: an accidentally
# leaked/copy-pasted CHAOS_RESET_STATE=0 must be visible in progress.log,
# not just inferable from the absence of "[reset]" lines (see
# analysis/PREREGISTRATION.md amendment 3 for why silent no-reset is a
# validity-breaking failure mode, not a minor one).
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

            # State reset before every rep: repetitions are only i.i.d. from
            # identical app state (pilot: rep 10 ran at 11 rps / 74% errors
            # without this). CHAOS_RESET_STATE=0 to disable. CHAOS_SLOT is
            # already exported above, so reset-app-state.sh resets the
            # correct slot's namespace.
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
