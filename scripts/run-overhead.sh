#!/usr/bin/env bash
################################################################################
# Overhead-Isolation Batch Runner
#
# Drives run-experiment.py --mode overhead through its three configurations,
# 10 reps each, IN THIS ORDER, because config (a) is only valid measurement
# if the chaos tool is not installed on the cluster at all:
#
#   (a) baseline      no chaos tool installed, no fault, 300s load window
#   (b) idle          <tool> installed and running (agents up), no fault
#   (c) fault         <tool> installed, existing 4-phase benchmark protocol
#                      for one representative scenario (default p1)
#
# This script enforces that ordering: before running stage (a) it checks
# that neither the chaos-mesh nor litmus namespace exists (refuses to run
# otherwise, since a leftover install from a prior study would silently
# contaminate the "no tool installed" baseline), and it stops with an
# interactive prompt between (a) and (b) so you can install the tool by
# hand (`helm install ...` / `./scripts/setup.sh`, whichever your cluster
# uses -- deliberately not automated here, since installing the tool is
# owned by setup.sh in another workstream) before continuing.
#
# Usage:
#   ./scripts/run-overhead.sh --tool chaos-mesh|litmus [--reps N] [--scenario ID] [--stage baseline|idle|fault|all]
#
# Examples:
#   ./scripts/run-overhead.sh --tool chaos-mesh                     # all 3 stages, 10 reps each
#   ./scripts/run-overhead.sh --tool chaos-mesh --stage baseline    # just stage (a)
#   ./scripts/run-overhead.sh --tool litmus --stage idle --reps 10  # just stage (b), resumable
#   ./scripts/run-overhead.sh --tool litmus --stage fault --scenario p1
#
# Resume: same as run-all-experiments.sh -- each stage skips any
# data/overhead/{config}/run-N.json that already exists, so re-running this
# script (or re-running a single --stage) after an interruption is safe.
################################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

export AWS_PROFILE="${AWS_PROFILE:-is-staging-mfa}"

DATA_DIR="${CHAOS_DATA_DIR:-${PROJECT_ROOT}/data}"
PROGRESS_LOG="${DATA_DIR}/progress.log"

TOOL=""
REPS=10
SCENARIO="p1"
STAGE="all"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tool)
            TOOL="${2:-}"
            shift 2
            ;;
        --reps)
            REPS="${2:-}"
            shift 2
            ;;
        --scenario)
            SCENARIO="${2:-}"
            shift 2
            ;;
        --stage)
            STAGE="${2:-}"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 --tool chaos-mesh|litmus [--reps N] [--scenario ID] [--stage baseline|idle|fault|all]"
            exit 0
            ;;
        *)
            echo "ERROR: Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

case "${TOOL}" in
    chaos-mesh|litmus) ;;
    *)
        echo "ERROR: --tool must be chaos-mesh or litmus (got '${TOOL}')" >&2
        exit 1
        ;;
esac

case "${STAGE}" in
    baseline|idle|fault|all) ;;
    *)
        echo "ERROR: --stage must be baseline, idle, fault, or all (got '${STAGE}')" >&2
        exit 1
        ;;
esac

if ! [[ "${REPS}" =~ ^[0-9]+$ ]] || [[ "${REPS}" -lt 1 ]]; then
    echo "ERROR: --reps must be a positive integer (got '${REPS}')" >&2
    exit 1
fi

mkdir -p "${DATA_DIR}"

log() {
    echo "$1" | tee -a "${PROGRESS_LOG}"
}

confirm() {
    local prompt="$1"
    # CHAOS_ASSUME_YES=1 for unattended runs (watchdog-supervised campaigns):
    # the safety ordering is still enforced by check_no_tool_installed; the
    # prompt exists for interactive use, and read(1) cannot work without a tty.
    if [[ "${CHAOS_ASSUME_YES:-0}" == "1" ]]; then
        echo "${prompt} [auto-confirmed: CHAOS_ASSUME_YES=1]"
        return 0
    fi
    read -r -p "${prompt} [y/N] " reply
    case "${reply}" in
        y|Y|yes|YES) ;;
        *)
            echo "Aborted."
            exit 1
            ;;
    esac
}

run_stage() {
    local overhead_config="$1"
    local extra_args=()
    if [[ "${overhead_config}" != "baseline" ]]; then
        extra_args+=(--tool "${TOOL}")
    fi
    if [[ "${overhead_config}" == "fault" ]]; then
        extra_args+=(--scenario "${SCENARIO}")
    fi

    log ""
    log "================================================================================"
    log "  Overhead stage: ${overhead_config} (tool=${TOOL:-none}, reps=1-${REPS})"
    log "  Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    log "================================================================================"

    local failed=0
    for run in $(seq 1 "${REPS}"); do
        local config_label
        if [[ "${overhead_config}" == "baseline" ]]; then
            config_label="baseline"
        else
            config_label="${TOOL}-${overhead_config}"
        fi
        local output_file="${DATA_DIR}/overhead/${config_label}/run-${run}.json"

        if [[ -f "${output_file}" ]]; then
            log "[${overhead_config} ${run}/${REPS}] SKIP (exists: ${output_file})"
            continue
        fi

        # Per-rep state reset (see run-all-experiments.sh); CHAOS_RESET_STATE=0 disables.
        if [[ "${CHAOS_RESET_STATE:-1}" == "1" ]]; then
            log "[${overhead_config} ${run}/${REPS}] resetting app state..."
            if ! "${SCRIPT_DIR}/reset-app-state.sh" >> "${PROGRESS_LOG}" 2>&1; then
                log "[${overhead_config} ${run}/${REPS}] FAIL (state reset failed)"
                failed=$((failed + 1))
                continue
            fi
        fi

        log "[${overhead_config} ${run}/${REPS}] RUN  ${config_label}"
        if python3 "${SCRIPT_DIR}/run-experiment.py" \
            --mode overhead \
            --overhead-config "${overhead_config}" \
            --run "${run}" \
            "${extra_args[@]}" 2>&1 | tee -a "${PROGRESS_LOG}"; then
            log "[${overhead_config} ${run}/${REPS}] PASS"
        else
            log "[${overhead_config} ${run}/${REPS}] FAIL"
            failed=$((failed + 1))
        fi
    done

    if [[ ${failed} -gt 0 ]]; then
        log "  Stage ${overhead_config}: ${failed} rep(s) FAILED."
        return 1
    fi
    return 0
}

check_no_tool_installed() {
    local found=()
    # chaos-testing is Chaos Mesh's actual install namespace; "chaos-mesh"
    # kept for safety with nonstandard installs.
    for ns in chaos-mesh chaos-testing litmus; do
        if kubectl get namespace "${ns}" >/dev/null 2>&1; then
            found+=("${ns}")
        fi
    done
    if [[ ${#found[@]} -gt 0 ]]; then
        echo "ERROR: Refusing to run the (a) baseline stage: namespace(s) ${found[*]} exist." >&2
        echo "       Baseline must be measured with NO chaos tool installed on the cluster." >&2
        echo "       Tear them down (./scripts/teardown.sh or equivalent) and re-run." >&2
        exit 1
    fi
}

run_baseline_stage() {
    check_no_tool_installed
    confirm "No chaos-mesh/litmus namespace detected. Run ${REPS} baseline (no-tool) reps now?"
    run_stage baseline
}

run_idle_stage() {
    confirm "About to run ${REPS} ${TOOL}-idle reps. Confirm ${TOOL} is installed and its agents are Running, with NO fault active."
    run_stage idle
}

run_fault_stage() {
    confirm "About to run ${REPS} ${TOOL}-fault reps (scenario ${SCENARIO}, full 4-phase protocol). Continue?"
    run_stage fault
}

case "${STAGE}" in
    baseline)
        run_baseline_stage
        ;;
    idle)
        run_idle_stage
        ;;
    fault)
        run_fault_stage
        ;;
    all)
        run_baseline_stage
        echo ""
        echo "Stage (a) baseline complete."
        echo "Now install ${TOOL} on this cluster (helm install / ./scripts/setup.sh, whichever"
        echo "this cluster uses) so its agents are up and idle, THEN continue."
        confirm "Has ${TOOL} been installed and its agent pods are Running?"
        run_idle_stage
        run_fault_stage
        ;;
esac

log ""
log "================================================================================"
log "  Overhead batch (--stage ${STAGE}, tool=${TOOL}) complete: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
log "================================================================================"
