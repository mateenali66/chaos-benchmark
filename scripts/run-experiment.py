#!/usr/bin/env python3
"""
Chaos Benchmark Experiment Runner

--mode benchmark (default, unchanged from the original protocol):
  Orchestrates a single experiment run for one (tool, scenario, rep):
    1. Baseline (5 min) - steady-state metrics under load
    2. Fault injection (2 min) - apply chaos experiment
    3. Recovery (1 min) - remove fault, measure TTR
    4. Cooldown (1 min) - ensure stability
  Results: data/{tool}/{scenario}/run-N.json (N now goes up to 30, see
  scripts/run-all-experiments.sh --reps).

--mode overhead:
  Runs one rep of one of three overhead-isolation configurations used to
  separate "cost of running the chaos tool" from "cost of the fault itself":
    --overhead-config baseline  10s wrk2 warm-up + 300s load, NO chaos tool
                                 installed, NO fault. Caller (run-overhead.sh)
                                 must verify the chaos-mesh/litmus namespaces
                                 do not exist before invoking this.
    --overhead-config idle      same 300s load window, chaos tool IS
                                 installed (agents running) but idle, no fault.
    --overhead-config fault     the existing 4-phase benchmark protocol for a
                                 single representative scenario (default p1).
  Results: data/overhead/baseline/run-N.json,
           data/overhead/{tool}-idle/run-N.json,
           data/overhead/{tool}-fault/run-N.json

Every run (either mode) also writes a gzipped raw-Prometheus-timeseries
sidecar next to its JSON (run-N.timeseries.json.gz) for the ML
anomaly-detection analysis; see scripts/CAMPAIGN_MODES.md.

Usage:
  python3 run-experiment.py --tool chaos-mesh --scenario p1 --run 1
  python3 run-experiment.py --tool litmus --scenario n3 --run 3 --dry-run
  python3 run-experiment.py --mode overhead --overhead-config baseline --run 1
  python3 run-experiment.py --mode overhead --overhead-config idle --tool chaos-mesh --run 1
  python3 run-experiment.py --mode overhead --overhead-config fault --tool chaos-mesh --scenario p1 --run 1
"""

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import chaoslib

################################################################################
# Main Experiment Orchestration
################################################################################

def run_experiment(tool: str, scenario: str, run_number: int, dry_run: bool = False):
    """Run a single --mode benchmark experiment iteration (original protocol,
    untouched apart from importing shared logic from chaoslib and allowing
    run numbers up to 30)."""
    scenario_lower = scenario.lower()
    scenario_file = chaoslib.SCENARIOS.get(scenario_lower)
    if not scenario_file:
        print(f"ERROR: Unknown scenario '{scenario}'. Valid: {', '.join(chaoslib.SCENARIOS.keys())}", file=sys.stderr)
        sys.exit(1)

    experiment_path = chaoslib.EXPERIMENTS_DIR / tool / f"{scenario_file}.yaml"
    if not experiment_path.exists():
        print(f"ERROR: Experiment file not found: {experiment_path}", file=sys.stderr)
        sys.exit(1)

    output_dir = chaoslib.DATA_DIR / tool / scenario_lower
    output_file = output_dir / f"run-{run_number}.json"

    if output_file.exists():
        print(f"  Output already exists: {output_file}. Skipping.")
        return

    print(f"\n{'='*80}")
    print(f"  Experiment: {tool} / {scenario_lower} / run {run_number}")
    print(f"  File: {experiment_path}")
    print(f"  Output: {output_file}")
    print(f"{'='*80}")

    if dry_run:
        print("  [DRY RUN] Would execute experiment. Exiting.")
        wrk2_yaml = chaoslib.render_wrk2_job(scenario_lower, run_number, chaoslib.TOTAL_LOAD_DURATION)
        print(f"\n  wrk2 Job YAML:\n{wrk2_yaml}")
        return

    # Ensure output directory exists
    output_dir.mkdir(parents=True, exist_ok=True)

    # Start port-forward to Prometheus
    print("\n  Starting Prometheus port-forward...")
    try:
        chaoslib.start_port_forward("monitoring", "prometheus-kube-prometheus-prometheus",
                                     chaoslib.PROMETHEUS_PORT, 9090)
        prom_available = True
    except RuntimeError as e:
        print(f"  WARNING: {e}. Prometheus metrics will be unavailable.", file=sys.stderr)
        prom_available = False

    results = {
        "metadata": {
            "tool": tool,
            "scenario": scenario_lower,
            "run": run_number,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cluster": os.environ.get("CHAOS_CLUSTER_NAME", Path(os.environ.get("CHAOS_DATA_DIR", "chaos-benchmark")).name),
            "experiment_file": str(experiment_path.relative_to(chaoslib.PROJECT_ROOT)),
            "protocol": {
                "baseline_s": chaoslib.BASELINE_DURATION,
                "fault_s": chaoslib.FAULT_DURATION,
                "recovery_s": chaoslib.RECOVERY_DURATION,
                "cooldown_s": chaoslib.COOLDOWN_DURATION,
            },
        },
        "wrk2": {},
        "phases": {},
        "derived": {},
    }
    window = None

    try:
        protocol_results = chaoslib.run_fault_protocol(
            experiment_path=experiment_path,
            label=scenario_lower,
            run_number=run_number,
            baseline_s=chaoslib.BASELINE_DURATION,
            fault_s=chaoslib.FAULT_DURATION,
            recovery_s=chaoslib.RECOVERY_DURATION,
            cooldown_s=chaoslib.COOLDOWN_DURATION,
            prom_available=prom_available,
            post_fault_restart=chaoslib.POST_FAULT_RESTART.get(scenario_lower),
        )
        window = protocol_results.pop("_window", None)
        results.update(protocol_results)
    except KeyboardInterrupt:
        print("\n  Interrupted by user. Cleaning up...")
        chaoslib.kubectl_delete_file(str(experiment_path))
    except Exception as e:
        print(f"\n  ERROR: {e}", file=sys.stderr)
        results["error"] = str(e)
    finally:
        chaoslib.cleanup_port_forwards()

    # Save results
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n  Results saved to: {output_file}")

    # Timeseries sidecar for the ML anomaly-detection analysis (best-effort;
    # never affects run success/failure -- see chaoslib.write_timeseries_sidecar).
    if window is not None:
        chaoslib.write_timeseries_sidecar(
            output_file, window["start"], window["end"],
            window["fault_start"], window["fault_end"],
            prom_available=prom_available,
        )

    # Print summary
    wrk2 = results.get("wrk2", {})
    derived = results.get("derived", {})
    print(f"\n  Summary:")
    print(f"    Throughput:    {wrk2.get('throughput_rps', 'N/A')} req/s")
    print(f"    Latency p99:   {wrk2.get('latency_ms', {}).get('p99', 'N/A')} ms")
    print(f"    Pod restarts:  {derived.get('pod_restarts_during_fault', 'N/A')}")
    print(f"    CPU spike:     {derived.get('cpu_spike_pct', 'N/A')}%")


def run_overhead(overhead_config: str, tool: str | None, scenario: str | None,
                  run_number: int, dry_run: bool = False):
    """Run a single --mode overhead rep. See module docstring for the three
    configs. Config folder names are a deliberate departure from the literal
    "data/overhead/{config}/run-N.json" wording in the brief: "baseline" has
    no tool (one shared 10-rep set is enough, it never varies by tool), while
    "idle" and "fault" fold the --tool into the folder name
    (data/overhead/{tool}-idle/, data/overhead/{tool}-fault/) so that running
    the overhead study for both tools never overwrites the other's data. See
    scripts/CAMPAIGN_MODES.md for the full rationale.
    """
    if overhead_config == "baseline":
        config_label = "baseline"
        if tool:
            print("  NOTE: --tool is ignored for --overhead-config baseline (no tool installed).")
    elif overhead_config in ("idle", "fault"):
        if not tool:
            print(f"ERROR: --overhead-config {overhead_config} requires --tool", file=sys.stderr)
            sys.exit(1)
        config_label = f"{tool}-{overhead_config}"
    else:
        print(f"ERROR: Unknown --overhead-config '{overhead_config}'", file=sys.stderr)
        sys.exit(1)

    output_dir = chaoslib.DATA_DIR / "overhead" / config_label
    output_file = output_dir / f"run-{run_number}.json"

    if output_file.exists():
        print(f"  Output already exists: {output_file}. Skipping.")
        return

    print(f"\n{'='*80}")
    print(f"  Overhead experiment: {config_label} / run {run_number}")
    print(f"  Output: {output_file}")
    print(f"{'='*80}")

    if dry_run:
        print("  [DRY RUN] Would execute overhead experiment. Exiting.")
        return

    output_dir.mkdir(parents=True, exist_ok=True)

    print("\n  Starting Prometheus port-forward...")
    try:
        chaoslib.start_port_forward("monitoring", "prometheus-kube-prometheus-prometheus",
                                     chaoslib.PROMETHEUS_PORT, 9090)
        prom_available = True
    except RuntimeError as e:
        print(f"  WARNING: {e}. Prometheus metrics will be unavailable.", file=sys.stderr)
        prom_available = False

    results = {
        "metadata": {
            "mode": "overhead",
            "overhead_config": overhead_config,
            "tool": tool,
            "scenario": scenario.lower() if (overhead_config == "fault" and scenario) else None,
            "run": run_number,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cluster": os.environ.get("CHAOS_CLUSTER_NAME", Path(os.environ.get("CHAOS_DATA_DIR", "chaos-benchmark")).name),
        },
        "wrk2": {},
        "phases": {},
        "derived": {},
    }
    window = None

    try:
        if overhead_config == "fault":
            scenario_lower = (scenario or "p1").lower()
            scenario_file = chaoslib.SCENARIOS.get(scenario_lower)
            if not scenario_file:
                print(f"ERROR: Unknown scenario '{scenario_lower}'. Valid: {', '.join(chaoslib.SCENARIOS.keys())}",
                      file=sys.stderr)
                sys.exit(1)
            experiment_path = chaoslib.EXPERIMENTS_DIR / tool / f"{scenario_file}.yaml"
            if not experiment_path.exists():
                print(f"ERROR: Experiment file not found: {experiment_path}", file=sys.stderr)
                sys.exit(1)
            results["metadata"]["scenario"] = scenario_lower
            results["metadata"]["protocol"] = {
                "baseline_s": chaoslib.BASELINE_DURATION,
                "fault_s": chaoslib.FAULT_DURATION,
                "recovery_s": chaoslib.RECOVERY_DURATION,
                "cooldown_s": chaoslib.COOLDOWN_DURATION,
            }
            protocol_results = chaoslib.run_fault_protocol(
                experiment_path=experiment_path,
                label=config_label,
                run_number=run_number,
                baseline_s=chaoslib.BASELINE_DURATION,
                fault_s=chaoslib.FAULT_DURATION,
                recovery_s=chaoslib.RECOVERY_DURATION,
                cooldown_s=chaoslib.COOLDOWN_DURATION,
                prom_available=prom_available,
                post_fault_restart=chaoslib.POST_FAULT_RESTART.get(scenario_lower),
            )
        else:
            # baseline / idle: flat load window, no fault, no chaos manifest.
            results["metadata"]["protocol"] = {"load_s": chaoslib.OVERHEAD_LOAD_DURATION}
            protocol_results = chaoslib.run_flat_load(
                label=config_label,
                run_number=run_number,
                duration_s=chaoslib.OVERHEAD_LOAD_DURATION,
                prom_available=prom_available,
            )
        window = protocol_results.pop("_window", None)
        results.update(protocol_results)
    except KeyboardInterrupt:
        print("\n  Interrupted by user. Cleaning up...")
    except Exception as e:
        print(f"\n  ERROR: {e}", file=sys.stderr)
        results["error"] = str(e)
    finally:
        chaoslib.cleanup_port_forwards()

    with open(output_file, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n  Results saved to: {output_file}")

    if window is not None:
        chaoslib.write_timeseries_sidecar(
            output_file, window["start"], window["end"],
            window["fault_start"], window["fault_end"],
            prom_available=prom_available,
        )

    wrk2 = results.get("wrk2", {})
    print(f"\n  Summary:")
    print(f"    Throughput:    {wrk2.get('throughput_rps', 'N/A')} req/s")
    print(f"    Latency p99:   {wrk2.get('latency_ms', {}).get('p99', 'N/A')} ms")

################################################################################
# Entry Point
################################################################################

def parse_args():
    parser = argparse.ArgumentParser(
        description="Chaos Benchmark Experiment Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --tool chaos-mesh --scenario p1 --run 1
  %(prog)s --tool litmus --scenario n3 --run 3 --dry-run
  %(prog)s --tool chaos-mesh --scenario a1 --run 2
  %(prog)s --mode overhead --overhead-config baseline --run 1
  %(prog)s --mode overhead --overhead-config idle --tool chaos-mesh --run 1
  %(prog)s --mode overhead --overhead-config fault --tool chaos-mesh --scenario p1 --run 1
        """,
    )
    parser.add_argument("--mode", choices=["benchmark", "overhead"], default="benchmark",
                        help="benchmark = original 4-phase protocol (default); "
                             "overhead = overhead-isolation study (see scripts/CAMPAIGN_MODES.md)")
    parser.add_argument("--tool", choices=["chaos-mesh", "litmus"],
                        help="Chaos engineering tool (required for --mode benchmark; "
                             "required for --mode overhead unless --overhead-config baseline)")
    parser.add_argument("--scenario",
                        help=f"Scenario ID: {', '.join(chaoslib.SCENARIOS.keys())} "
                             "(required for --mode benchmark; optional for --overhead-config fault, default p1)")
    parser.add_argument("--run", required=True, type=int,
                        help="Run number (1-30 for --mode benchmark, 1-10 for --mode overhead)")
    parser.add_argument("--overhead-config", choices=["baseline", "idle", "fault"],
                        help="Required for --mode overhead")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without executing")
    return parser.parse_args()


def main():
    args = parse_args()

    # Register signal handler for graceful cleanup
    def signal_handler(sig, frame):
        print("\nReceived interrupt signal. Cleaning up...")
        chaoslib.cleanup_port_forwards()
        sys.exit(1)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if args.mode == "benchmark":
        if not args.tool or not args.scenario:
            print("ERROR: --mode benchmark requires --tool and --scenario", file=sys.stderr)
            sys.exit(1)
        if args.scenario.lower() not in chaoslib.SCENARIOS:
            print(f"ERROR: Unknown scenario '{args.scenario}'. Valid: {', '.join(chaoslib.SCENARIOS.keys())}",
                  file=sys.stderr)
            sys.exit(1)
        if args.run < 1 or args.run > 30:
            print(f"ERROR: Run number must be 1-30, got {args.run}", file=sys.stderr)
            sys.exit(1)
        run_experiment(args.tool, args.scenario, args.run, args.dry_run)
    else:  # overhead
        if not args.overhead_config:
            print("ERROR: --mode overhead requires --overhead-config", file=sys.stderr)
            sys.exit(1)
        if args.scenario and args.scenario.lower() not in chaoslib.SCENARIOS:
            print(f"ERROR: Unknown scenario '{args.scenario}'. Valid: {', '.join(chaoslib.SCENARIOS.keys())}",
                  file=sys.stderr)
            sys.exit(1)
        if args.run < 1 or args.run > 10:
            print(f"ERROR: Run number must be 1-10 for --mode overhead, got {args.run}", file=sys.stderr)
            sys.exit(1)
        run_overhead(args.overhead_config, args.tool, args.scenario, args.run, args.dry_run)


if __name__ == "__main__":
    main()
