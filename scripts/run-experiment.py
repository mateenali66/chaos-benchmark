#!/usr/bin/env python3
"""
Chaos Benchmark Experiment Runner

Orchestrates a single experiment run:
  1. Baseline (5 min) - steady-state metrics under load
  2. Fault injection (2 min) - apply chaos experiment
  3. Recovery (1 min) - remove fault, measure TTR
  4. Cooldown (1 min) - ensure stability

Usage:
  python3 run-experiment.py --tool chaos-mesh --scenario p1 --run 1
  python3 run-experiment.py --tool litmus --scenario n3 --run 3 --dry-run
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from string import Template

################################################################################
# Configuration
################################################################################

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXPERIMENTS_DIR = PROJECT_ROOT / "experiments"
DATA_DIR = PROJECT_ROOT / "data"
WRK2_TEMPLATE = PROJECT_ROOT / "load-generator" / "wrk2-job.yaml.tpl"

ECR_REPO = "886604922358.dkr.ecr.us-east-1.amazonaws.com/chaos-benchmark/wrk2"
NAMESPACE = "social-network"

# Protocol timing (seconds)
BASELINE_DURATION = 300
FAULT_DURATION = 120
RECOVERY_DURATION = 60
COOLDOWN_DURATION = 60
TOTAL_LOAD_DURATION = BASELINE_DURATION + FAULT_DURATION + RECOVERY_DURATION

# Load generator defaults
WRK_THREADS = "4"
WRK_CONNECTIONS = "100"
WRK_RATE = "200"

# Prometheus
PROMETHEUS_PORT = 9090

# Scenarios mapping (lowercase scenario ID -> filename)
SCENARIOS = {
    "p1": "p1-pod-kill",
    "p2": "p2-container-kill",
    "p3": "p3-pod-failure",
    "n1": "n1-latency-50ms",
    "n2": "n2-latency-100ms",
    "n3": "n3-latency-300ms",
    "n4": "n4-packet-loss-5pct",
    "n5": "n5-network-partition",
    "r1": "r1-cpu-stress-80pct",
    "r2": "r2-memory-pressure-80pct",
    "a1": "a1-http-abort-503",
    "a2": "a2-grpc-unavailable",
}

INFRA_QUERIES = {
    "cpu_usage": 'sum(rate(container_cpu_usage_seconds_total{{namespace="{ns}"}}[30s])) by (pod)',
    "memory_usage": 'sum(container_memory_working_set_bytes{{namespace="{ns}"}}) by (pod)',
    "pod_restarts": 'sum(kube_pod_container_status_restarts_total{{namespace="{ns}"}}) by (pod)',
    "network_rx_bytes": 'sum(rate(container_network_receive_bytes_total{{namespace="{ns}"}}[30s])) by (pod)',
}

################################################################################
# Subprocess Helpers
################################################################################

def run_cmd(cmd: list[str], timeout: int = 120, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
        return result
    except subprocess.TimeoutExpired:
        print(f"  TIMEOUT: {' '.join(cmd[:3])}...", file=sys.stderr)
        raise


def kubectl_apply_file(path: str) -> bool:
    """Apply a Kubernetes manifest file."""
    result = run_cmd(["kubectl", "apply", "-f", path])
    if result.returncode != 0:
        print(f"  kubectl apply failed: {result.stderr}", file=sys.stderr)
        return False
    return True


def kubectl_apply_stdin(yaml_str: str) -> bool:
    """Apply a Kubernetes manifest from stdin."""
    result = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=yaml_str,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        print(f"  kubectl apply failed: {result.stderr}", file=sys.stderr)
        return False
    return True


def kubectl_delete_file(path: str) -> bool:
    """Delete resources defined in a manifest file."""
    result = run_cmd(["kubectl", "delete", "-f", path, "--ignore-not-found=true"], timeout=60)
    if result.returncode != 0:
        print(f"  kubectl delete failed: {result.stderr}", file=sys.stderr)
        return False
    return True


def kubectl_delete_stdin(yaml_str: str) -> bool:
    """Delete resources from stdin YAML."""
    result = subprocess.run(
        ["kubectl", "delete", "-f", "-", "--ignore-not-found=true"],
        input=yaml_str,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode == 0


def kubectl_logs(job_name: str, namespace: str, timeout: int = 30) -> str:
    """Get logs from a Job's pod."""
    result = run_cmd(
        ["kubectl", "logs", f"job/{job_name}", "-n", namespace],
        timeout=timeout,
    )
    return result.stdout if result.returncode == 0 else ""

################################################################################
# Port-Forward Management
################################################################################

_port_forwards: list[subprocess.Popen] = []


def start_port_forward(namespace: str, service: str, local_port: int, remote_port: int) -> subprocess.Popen:
    """Start a kubectl port-forward in the background."""
    proc = subprocess.Popen(
        ["kubectl", "port-forward", f"svc/{service}", f"{local_port}:{remote_port}", "-n", namespace],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _port_forwards.append(proc)
    time.sleep(3)
    if proc.poll() is not None:
        raise RuntimeError(f"Port-forward to {service}:{remote_port} failed to start")

    # Health check: verify endpoint is actually responding
    if local_port == PROMETHEUS_PORT:
        for attempt in range(10):
            try:
                resp = query_prometheus_instant("up")
                if resp.get("status") == "success":
                    break
            except Exception:
                pass
            time.sleep(2)
        else:
            print("  WARNING: Prometheus health check failed after 20s", file=sys.stderr)

    return proc


def cleanup_port_forwards():
    """Kill all port-forward processes."""
    for proc in _port_forwards:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    _port_forwards.clear()

################################################################################
# Prometheus Queries
################################################################################

def query_prometheus_range(query: str, start: float, end: float, step: str = "15s") -> dict:
    """Query Prometheus range API."""
    params = urllib.parse.urlencode({
        "query": query,
        "start": str(start),
        "end": str(end),
        "step": step,
    })
    url = f"http://localhost:{PROMETHEUS_PORT}/api/v1/query_range?{params}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"  Prometheus query failed: {e}", file=sys.stderr)
        return {"status": "error", "data": {"result": []}}


def query_prometheus_instant(query: str) -> dict:
    """Query Prometheus instant API."""
    params = urllib.parse.urlencode({"query": query})
    url = f"http://localhost:{PROMETHEUS_PORT}/api/v1/query?{params}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"  Prometheus query failed: {e}", file=sys.stderr)
        return {"status": "error", "data": {"result": []}}


def collect_infra_metrics(start_ts: float, end_ts: float) -> dict:
    """Collect infrastructure metrics from Prometheus for a time window."""
    metrics = {}
    for name, query_tpl in INFRA_QUERIES.items():
        query = query_tpl.format(ns=NAMESPACE)
        result = query_prometheus_range(query, start_ts, end_ts)
        if result.get("status") == "success":
            metrics[name] = result["data"]["result"]
        else:
            metrics[name] = []
    return metrics

################################################################################
# wrk2 Job Management
################################################################################

def render_wrk2_job(scenario: str, run: int, duration: int) -> str:
    """Render wrk2 Job YAML from template."""
    with open(WRK2_TEMPLATE) as f:
        template = f.read()

    job_name = f"wrk2-{scenario}-run{run}"
    substitutions = {
        "JOB_NAME": job_name,
        "ECR_REPO": ECR_REPO,
        "WRK_DURATION": str(duration),
        "WRK_RATE": WRK_RATE,
        "WRK_THREADS": WRK_THREADS,
        "WRK_CONNECTIONS": WRK_CONNECTIONS,
    }

    # Use simple string replacement for ${VAR} placeholders
    rendered = template
    for key, value in substitutions.items():
        rendered = rendered.replace(f"${{{key}}}", value)

    return rendered


def wait_for_job(job_name: str, namespace: str, timeout: int = 600) -> bool:
    """Wait for a Kubernetes Job to complete."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = run_cmd([
            "kubectl", "get", "job", job_name, "-n", namespace,
            "-o", "jsonpath={.status.conditions[?(@.type=='Complete')].status}"
        ])
        if result.stdout.strip() == "True":
            return True

        # Check for failure
        fail_result = run_cmd([
            "kubectl", "get", "job", job_name, "-n", namespace,
            "-o", "jsonpath={.status.conditions[?(@.type=='Failed')].status}"
        ])
        if fail_result.stdout.strip() == "True":
            print(f"  Job {job_name} FAILED", file=sys.stderr)
            return False

        time.sleep(10)

    print(f"  Job {job_name} timed out after {timeout}s", file=sys.stderr)
    return False


def cleanup_wrk2_job(job_name: str, namespace: str):
    """Delete a wrk2 Job and its pods."""
    run_cmd(["kubectl", "delete", "job", job_name, "-n", namespace, "--ignore-not-found=true"], timeout=30)

################################################################################
# wrk2 Output Parser
################################################################################

def parse_wrk2_output(log_text: str) -> dict:
    """Parse wrk2 output for latency percentiles, throughput, and errors."""
    result = {
        "throughput_rps": 0.0,
        "latency_ms": {"p50": 0.0, "p95": 0.0, "p99": 0.0, "p999": 0.0},
        "errors": {"connect": 0, "read": 0, "write": 0, "timeout": 0, "http_non2xx3xx": 0},
        "duration_s": 0,
        "requests_total": 0,
    }

    if not log_text:
        print("  WARNING: wrk2 log output is empty", file=sys.stderr)
        return result

    # Show preview of raw output for debugging
    preview = log_text.strip()[-500:] if len(log_text) > 500 else log_text.strip()
    print(f"  wrk2 output preview:\n    {preview[:200]}...")

    # Throughput: "Requests/sec:   987.50"
    m = re.search(r'Requests/sec:\s+([\d.]+)', log_text)
    if m:
        result["throughput_rps"] = float(m.group(1))

    # Total requests: "12345 requests in 30.00s"
    m = re.search(r'(\d+)\s+requests\s+in\s+([\d.]+)([smh])', log_text)
    if m:
        result["requests_total"] = int(m.group(1))
        dur_val = float(m.group(2))
        dur_unit = m.group(3)
        if dur_unit == "m":
            dur_val *= 60
        elif dur_unit == "h":
            dur_val *= 3600
        result["duration_s"] = int(dur_val)

    # Latency percentiles - try two formats:
    # Format A (standard wrk2): "50.000%    2.10ms"
    # Format B (wrk2 -L histogram): "206065.420     0.500000        29483       2.00"
    #   where col1=microseconds, col2=percentile fraction

    # Try Format A first (with unit suffixes)
    percentile_map = {
        "50.000": "p50",
        "95.000": "p95",
        "99.000": "p99",
        "99.900": "p999",
    }
    format_a_found = False
    for line in log_text.split("\n"):
        for pct_str, key in percentile_map.items():
            if pct_str + "%" in line:
                m = re.search(r'([\d.]+)(us|ms|s)', line)
                if m:
                    val = float(m.group(1))
                    unit = m.group(2)
                    if unit == "us":
                        val /= 1000.0
                    elif unit == "s":
                        val *= 1000.0
                    result["latency_ms"][key] = val
                    format_a_found = True

    # If Format A didn't work, try Format B (HdrHistogram detailed output)
    if not format_a_found:
        # Parse: "VALUE  PERCENTILE  COUNT  1/(1-PERCENTILE)"
        # Values are in microseconds, percentiles are fractions (0.500000 = p50)
        target_pcts = {"p50": 0.5, "p95": 0.95, "p99": 0.99, "p999": 0.999}
        best = {k: (None, 1.0) for k in target_pcts}  # key -> (value_us, distance)
        for line in log_text.split("\n"):
            parts = line.strip().split()
            if len(parts) >= 2:
                try:
                    val_us = float(parts[0])
                    pct = float(parts[1])
                    if 0.0 < pct <= 1.0 and val_us > 0:
                        for key, target in target_pcts.items():
                            dist = abs(pct - target)
                            if dist < best[key][1]:
                                best[key] = (val_us, dist)
                except ValueError:
                    continue
        for key, (val_us, _) in best.items():
            if val_us is not None:
                result["latency_ms"][key] = val_us / 1000.0  # convert us to ms

    # Also extract mean latency from histogram summary
    m = re.search(r'#\[Mean\s*=\s*([\d.]+)', log_text)
    if m:
        result["latency_ms"]["mean"] = float(m.group(1)) / 1000.0  # us to ms

    # Socket errors: "Socket errors: connect 0, read 3, write 0, timeout 1"
    m = re.search(r'Socket errors:\s*connect\s+(\d+),\s*read\s+(\d+),\s*write\s+(\d+),\s*timeout\s+(\d+)', log_text)
    if m:
        result["errors"]["connect"] = int(m.group(1))
        result["errors"]["read"] = int(m.group(2))
        result["errors"]["write"] = int(m.group(3))
        result["errors"]["timeout"] = int(m.group(4))

    # Non-2xx/3xx responses
    m = re.search(r'Non-2xx or 3xx responses:\s+(\d+)', log_text)
    if m:
        result["errors"]["http_non2xx3xx"] = int(m.group(1))

    return result

################################################################################
# Derived Metrics
################################################################################

def compute_derived_metrics(phases: dict) -> dict:
    """Compute derived metrics from phase data."""
    derived = {
        "pod_restarts_during_fault": 0,
        "cpu_spike_pct": 0.0,
        "memory_spike_mb": 0.0,
    }

    baseline_metrics = phases.get("baseline", {}).get("infra_metrics", {})
    fault_metrics = phases.get("fault", {}).get("infra_metrics", {})

    # Pod restarts during fault
    def sum_restart_values(metrics_data):
        total = 0
        for series in metrics_data.get("pod_restarts", []):
            values = series.get("values", [])
            if values:
                total += float(values[-1][1]) - float(values[0][1])
        return total

    derived["pod_restarts_during_fault"] = int(sum_restart_values(fault_metrics))

    # CPU spike: compare max fault CPU vs avg baseline CPU
    def avg_metric(metrics_data, key):
        total, count = 0.0, 0
        for series in metrics_data.get(key, []):
            for _, val in series.get("values", []):
                total += float(val)
                count += 1
        return total / count if count > 0 else 0.0

    def max_metric(metrics_data, key):
        max_val = 0.0
        for series in metrics_data.get(key, []):
            for _, val in series.get("values", []):
                max_val = max(max_val, float(val))
        return max_val

    baseline_cpu = avg_metric(baseline_metrics, "cpu_usage")
    fault_cpu_max = max_metric(fault_metrics, "cpu_usage")
    if baseline_cpu > 0:
        derived["cpu_spike_pct"] = round(((fault_cpu_max - baseline_cpu) / baseline_cpu) * 100, 1)

    # Memory spike in MB
    baseline_mem = avg_metric(baseline_metrics, "memory_usage")
    fault_mem_max = max_metric(fault_metrics, "memory_usage")
    derived["memory_spike_mb"] = round((fault_mem_max - baseline_mem) / (1024 * 1024), 1)

    return derived

################################################################################
# Main Experiment Orchestration
################################################################################

def run_experiment(tool: str, scenario: str, run_number: int, dry_run: bool = False):
    """Run a single experiment iteration."""
    scenario_lower = scenario.lower()
    scenario_file = SCENARIOS.get(scenario_lower)
    if not scenario_file:
        print(f"ERROR: Unknown scenario '{scenario}'. Valid: {', '.join(SCENARIOS.keys())}", file=sys.stderr)
        sys.exit(1)

    experiment_path = EXPERIMENTS_DIR / tool / f"{scenario_file}.yaml"
    if not experiment_path.exists():
        print(f"ERROR: Experiment file not found: {experiment_path}", file=sys.stderr)
        sys.exit(1)

    output_dir = DATA_DIR / tool / scenario_lower
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
        wrk2_yaml = render_wrk2_job(scenario_lower, run_number, TOTAL_LOAD_DURATION)
        print(f"\n  wrk2 Job YAML:\n{wrk2_yaml}")
        return

    # Ensure output directory exists
    output_dir.mkdir(parents=True, exist_ok=True)

    # Render wrk2 Job
    job_name = f"wrk2-{scenario_lower}-run{run_number}"
    wrk2_yaml = render_wrk2_job(scenario_lower, run_number, TOTAL_LOAD_DURATION)

    # Clean up any previous run artifacts
    cleanup_wrk2_job(job_name, NAMESPACE)
    kubectl_delete_file(str(experiment_path))
    time.sleep(5)

    # Start port-forward to Prometheus
    print("\n  Starting Prometheus port-forward...")
    try:
        prom_pf = start_port_forward("monitoring", "prometheus-kube-prometheus-prometheus", PROMETHEUS_PORT, 9090)
    except RuntimeError as e:
        print(f"  WARNING: {e}. Prometheus metrics will be unavailable.", file=sys.stderr)
        prom_pf = None

    results = {
        "metadata": {
            "tool": tool,
            "scenario": scenario_lower,
            "run": run_number,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cluster": "chaos-benchmark",
            "experiment_file": str(experiment_path.relative_to(PROJECT_ROOT)),
            "protocol": {
                "baseline_s": BASELINE_DURATION,
                "fault_s": FAULT_DURATION,
                "recovery_s": RECOVERY_DURATION,
                "cooldown_s": COOLDOWN_DURATION,
            },
        },
        "wrk2": {},
        "phases": {},
        "derived": {},
    }

    try:
        # Phase 1: Start wrk2 load generator (runs for entire baseline + fault + recovery)
        print(f"\n  [Phase 1] BASELINE ({BASELINE_DURATION}s) - Starting wrk2 load...")
        kubectl_apply_stdin(wrk2_yaml)
        time.sleep(10)  # wait for pod scheduling

        baseline_start = time.time()
        print(f"    Baseline started at {datetime.now().strftime('%H:%M:%S')}")
        print(f"    Waiting {BASELINE_DURATION}s for steady-state...")
        time.sleep(BASELINE_DURATION)
        baseline_end = time.time()

        results["phases"]["baseline"] = {
            "start": baseline_start,
            "end": baseline_end,
            "infra_metrics": collect_infra_metrics(baseline_start, baseline_end) if prom_pf else {},
        }
        print(f"    Baseline complete at {datetime.now().strftime('%H:%M:%S')}")

        # Phase 2: Inject fault
        print(f"\n  [Phase 2] FAULT ({FAULT_DURATION}s) - Injecting {scenario_lower}...")
        fault_start = time.time()
        kubectl_apply_file(str(experiment_path))
        print(f"    Fault injected at {datetime.now().strftime('%H:%M:%S')}")
        time.sleep(FAULT_DURATION)
        fault_end = time.time()

        results["phases"]["fault"] = {
            "start": fault_start,
            "end": fault_end,
            "infra_metrics": collect_infra_metrics(fault_start, fault_end) if prom_pf else {},
        }
        print(f"    Fault phase complete at {datetime.now().strftime('%H:%M:%S')}")

        # Phase 3: Recovery
        print(f"\n  [Phase 3] RECOVERY ({RECOVERY_DURATION}s) - Removing fault...")
        recovery_start = time.time()
        kubectl_delete_file(str(experiment_path))
        print(f"    Fault removed at {datetime.now().strftime('%H:%M:%S')}")

        # HTTPChaos experiments (a1, a2) leave stale iptables rules in the target
        # pod's network namespace. Restart the target deployment to clear them.
        if scenario_lower in ("a1", "a2"):
            target_deploy = "nginx-thrift" if scenario_lower == "a1" else "user-service"
            print(f"    Restarting {target_deploy} to clear stale network rules...")
            run_cmd(["kubectl", "rollout", "restart", f"deployment/{target_deploy}",
                     "-n", NAMESPACE], timeout=30)
            run_cmd(["kubectl", "rollout", "status", f"deployment/{target_deploy}",
                     "-n", NAMESPACE, "--timeout=60s"], timeout=90)
        time.sleep(RECOVERY_DURATION)
        recovery_end = time.time()

        results["phases"]["recovery"] = {
            "start": recovery_start,
            "end": recovery_end,
            "infra_metrics": collect_infra_metrics(recovery_start, recovery_end) if prom_pf else {},
        }
        print(f"    Recovery complete at {datetime.now().strftime('%H:%M:%S')}")

        # Phase 4: Cooldown (no metrics collection, just wait)
        print(f"\n  [Phase 4] COOLDOWN ({COOLDOWN_DURATION}s)...")
        time.sleep(COOLDOWN_DURATION)
        print(f"    Cooldown complete at {datetime.now().strftime('%H:%M:%S')}")

        # Collect wrk2 output
        print("\n  Collecting wrk2 results...")
        wait_for_job(job_name, NAMESPACE, timeout=120)
        wrk2_logs = kubectl_logs(job_name, NAMESPACE)
        results["wrk2"] = parse_wrk2_output(wrk2_logs)
        results["wrk2"]["raw_output"] = wrk2_logs[-5000:] if len(wrk2_logs) > 5000 else wrk2_logs

        # Compute derived metrics
        results["derived"] = compute_derived_metrics(results["phases"])

    except KeyboardInterrupt:
        print("\n  Interrupted by user. Cleaning up...")
        kubectl_delete_file(str(experiment_path))
    except Exception as e:
        print(f"\n  ERROR: {e}", file=sys.stderr)
        results["error"] = str(e)
    finally:
        # Cleanup
        cleanup_wrk2_job(job_name, NAMESPACE)
        kubectl_delete_file(str(experiment_path))
        cleanup_port_forwards()

    # Save results
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n  Results saved to: {output_file}")

    # Print summary
    wrk2 = results.get("wrk2", {})
    derived = results.get("derived", {})
    print(f"\n  Summary:")
    print(f"    Throughput:    {wrk2.get('throughput_rps', 'N/A')} req/s")
    print(f"    Latency p99:   {wrk2.get('latency_ms', {}).get('p99', 'N/A')} ms")
    print(f"    Pod restarts:  {derived.get('pod_restarts_during_fault', 'N/A')}")
    print(f"    CPU spike:     {derived.get('cpu_spike_pct', 'N/A')}%")

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
        """,
    )
    parser.add_argument("--tool", required=True, choices=["chaos-mesh", "litmus"],
                        help="Chaos engineering tool")
    parser.add_argument("--scenario", required=True,
                        help=f"Scenario ID: {', '.join(SCENARIOS.keys())}")
    parser.add_argument("--run", required=True, type=int,
                        help="Run number (1-5)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without executing")
    return parser.parse_args()


def main():
    args = parse_args()

    # Validate
    if args.scenario.lower() not in SCENARIOS:
        print(f"ERROR: Unknown scenario '{args.scenario}'. Valid: {', '.join(SCENARIOS.keys())}", file=sys.stderr)
        sys.exit(1)
    if args.run < 1 or args.run > 10:
        print(f"ERROR: Run number must be 1-10, got {args.run}", file=sys.stderr)
        sys.exit(1)

    # Register signal handler for graceful cleanup
    def signal_handler(sig, frame):
        print("\nReceived interrupt signal. Cleaning up...")
        cleanup_port_forwards()
        sys.exit(1)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    run_experiment(args.tool, args.scenario, args.run, args.dry_run)


if __name__ == "__main__":
    main()
