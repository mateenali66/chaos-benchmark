#!/usr/bin/env python3
"""
Chaos Benchmark shared library.

Common code for run-experiment.py, run-campaign.py and backfill-sidecars.py:
kubectl helpers, port-forward management, Prometheus queries (including the
full-run timeseries sidecar used by Component 5), wrk2 Job rendering and
parsing, derived metrics, and two protocol executors. run_fault_protocol runs
baseline, fault, recovery and an optional cooldown. run_flat_load runs one
load window with no fault, for the overhead baseline and idle configs.

It is a separate module because run-experiment.py has a hyphen in its name
and cannot be imported.
"""

import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

################################################################################
# Configuration
################################################################################

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXPERIMENTS_DIR = PROJECT_ROOT / "experiments"
# Results root, one per cluster: data-v2/bench-a, data-v2/bench-b or
# data-v2/ml. There is no default, because no single directory is right for
# every cluster and a wrong default writes valid-looking results into the
# wrong tree. The original 120-run dataset in data/ came from different
# infrastructure and is never mixed in.
_data_dir_env = os.environ.get("CHAOS_DATA_DIR")
if not _data_dir_env:
    raise RuntimeError(
        "CHAOS_DATA_DIR is not set. chaoslib.py has no default -- set it explicitly, "
        "e.g. export CHAOS_DATA_DIR=\"$(pwd)/data-v2/ml\" for Component 3 campaign work, "
        "or data-v2/bench-a / data-v2/bench-b for Component 1/2 work. "
        "(This check exists because a missing/wrong default silently misdirected three "
        "backfill runs on 2026-08-17 before anyone noticed -- see git log.)"
    )
DATA_DIR = Path(_data_dir_env)
WRK2_TEMPLATE = PROJECT_ROOT / "load-generator" / "wrk2-job.yaml.tpl"

# Set CHAOS_ECR_REPO to the image that build-wrk2-image.sh pushed. The default
# is a placeholder, so a run without it fails at image pull instead of pulling
# some other image.
ECR_REPO = os.environ.get(
    "CHAOS_ECR_REPO",
    "<ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/chaos-benchmark/wrk2",
)
NAMESPACE = "social-network"

# Slot parallelism: a cluster can run up to 3 isolated copies of the
# DeathStarBench stack, one per namespace, each on its own 3 nodes (node label
# chaos-slot=0/1/2). Slot 0, or CHAOS_SLOT unset, is the single-namespace
# default. See scripts/SLOT_PARALLELISM.md.
CHAOS_SLOT = os.environ.get("CHAOS_SLOT", "0")


def namespace_for_slot(slot: str | None) -> str:
    """Namespace for a slot: NAMESPACE for slot 0 or unset, "<NAMESPACE>-<N>" otherwise.

    reset-app-state.sh and init-social-graph.sh implement the same rule in
    bash. Change all three together.
    """
    return NAMESPACE if not slot or slot == "0" else f"{NAMESPACE}-{slot}"


# Per-slot local port offset, added to the per-cluster KUBECONFIG checksum
# offset that reset-app-state.sh and init-social-graph.sh use for their
# port-forwards. The checksum separates clusters; this separates slots that
# share one KUBECONFIG. The value is arbitrary but must match the literal
# `* 97` in those scripts.
SLOT_PORT_STEP = 97


def slot_port_offset(slot: str | None, base: int) -> int:
    """Local port for a slot, so slots on one KUBECONFIG do not collide.

    Slot 0 or unset returns base. Not used for PROMETHEUS_PORT: all slots
    share one Prometheus and differ only in the namespace they query.
    """
    if not slot or slot == "0":
        return base
    return base + int(slot) * SLOT_PORT_STEP


# Four-phase protocol timing in seconds, used by --mode benchmark
# (Component 1) and the fault config of --mode overhead.
BASELINE_DURATION = 300
FAULT_DURATION = 120
RECOVERY_DURATION = 60
COOLDOWN_DURATION = 60
TOTAL_LOAD_DURATION = BASELINE_DURATION + FAULT_DURATION + RECOVERY_DURATION

# Overhead baseline and idle configs: one 300 s load window, no fault.
OVERHEAD_LOAD_DURATION = 300

# Campaign mode (Component 3): shortened protocol, no cooldown.
CAMPAIGN_BASELINE_DURATION = 120
CAMPAIGN_FAULT_DURATION = 120
CAMPAIGN_RECOVERY_DURATION = 60

# Load generator defaults
WRK_THREADS = "4"
WRK_CONNECTIONS = "100"
# Offered load in requests per second. The application saturates at about
# 160-180 rps from a fresh state, where wrk2's corrected latency measures
# queueing rather than fault impact. 120 rps is about two thirds of that
# capacity (analysis/PREREGISTRATION.md, Component 1 amendment 4).
WRK_RATE = os.environ.get("CHAOS_LOAD_RPS", "120")

# Prometheus. CHAOS_PROM_PORT lets runners for two clusters port-forward on
# one machine at the same time (e.g. bench-a=9090, bench-b=9091).
PROMETHEUS_PORT = int(os.environ.get("CHAOS_PROM_PORT", "9090"))
TIMESERIES_STEP = "5s"

# Scenarios mapping (lowercase scenario ID -> filename), the original 12.
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

# Scenarios whose HTTPChaos fault leaves stale iptables rules behind and
# needs the target deployment restarted during recovery (see run_fault_protocol).
POST_FAULT_RESTART = {
    "a1": "nginx-thrift",
    "a2": "user-service",
}

INFRA_QUERIES = {
    "cpu_usage": 'sum(rate(container_cpu_usage_seconds_total{{namespace="{ns}", container!="", container!="POD"}}[30s])) by (pod)',
    "memory_usage": 'sum(container_memory_working_set_bytes{{namespace="{ns}", container!="", container!="POD"}}) by (pod)',
    "pod_restarts": 'sum(kube_pod_container_status_restarts_total{{namespace="{ns}", container!="", container!="POD"}}) by (pod)',
    # container_network_* is a pod-level cAdvisor metric with no "container"
    # label, so the container!=""/!="POD" filter used above would drop every
    # series. The [2m] window is needed for the same scrape-interval reason
    # as node_cpu_utilization in TIMESERIES_QUERIES. Runs collected before
    # this query was fixed have empty network series (see the erratum in
    # analysis/PREREGISTRATION.md, Component 5 features).
    "network_rx_bytes": 'sum(rate(container_network_receive_bytes_total{{namespace="{ns}"}}[2m])) by (pod)',
}

# Queries for the per-run timeseries sidecar (Component 5 input): the
# INFRA_QUERIES metrics plus node-level and best-effort L7 metrics. An empty
# result is not an error. DeathStarBench's services do not export Envoy-style
# L7 metrics without a service mesh, so the http_* entries are empty on these
# clusters.
TIMESERIES_QUERIES = {
    "container_cpu_usage": 'sum(rate(container_cpu_usage_seconds_total{{namespace="{ns}", container!="", container!="POD"}}[30s])) by (pod)',
    "container_memory_working_set": 'sum(container_memory_working_set_bytes{{namespace="{ns}", container!="", container!="POD"}}) by (pod)',
    # No container filter and a [2m] window, as in INFRA_QUERIES["network_rx_bytes"].
    "container_network_rx_bytes": 'sum(rate(container_network_receive_bytes_total{{namespace="{ns}"}}[2m])) by (pod)',
    "container_network_tx_bytes": 'sum(rate(container_network_transmit_bytes_total{{namespace="{ns}"}}[2m])) by (pod)',
    "pod_restarts_total": 'sum(kube_pod_container_status_restarts_total{{namespace="{ns}", container!="", container!="POD"}}) by (pod)',
    # node-exporter scrapes every 30s, so a [30s] rate window rarely holds the
    # two samples rate() needs and returns empty; [2m] is required here.
    "node_cpu_utilization": 'sum(rate(node_cpu_seconds_total{{mode!="idle"}}[2m])) by (instance) / count(node_cpu_seconds_total{{mode="idle"}}) by (instance)',
    "node_memory_used_bytes": "node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes",
    # Best-effort L7 metrics (see the comment above this dict).
    "http_request_rate": 'sum(rate(envoy_http_downstream_rq_total{{namespace="{ns}", container!="", container!="POD"}}[30s])) by (pod)',
    "http_error_rate": 'sum(rate(envoy_http_downstream_rq_xx{{namespace="{ns}", envoy_response_code_class!="2"}}[30s])) by (pod)',
    "http_latency_bucket": 'sum(rate(envoy_http_downstream_rq_time_bucket{{namespace="{ns}", container!="", container!="POD"}}[30s])) by (pod, le)',
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


def start_port_forward(namespace: str, service: str, local_port: int, remote_port: int) -> Optional[subprocess.Popen]:
    """Start a kubectl port-forward in the background and return the process.

    For PROMETHEUS_PORT, returns None without starting anything if Prometheus
    already answers there. All slots share one Prometheus on one local port,
    so a second slot would otherwise fail to bind, run with
    prom_available=False, and lose the per-phase metrics that
    run-campaign.py's recovery_over_60s signal reads. A process this call did
    not start is not added to _port_forwards, so cleanup_port_forwards() in
    one slot never stops another slot's tunnel. Raises RuntimeError if
    kubectl exits immediately.
    """
    if local_port == PROMETHEUS_PORT:
        try:
            if query_prometheus_instant("up").get("status") == "success":
                return None
        except Exception:
            pass

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
        return {"status": "error", "error": str(e), "data": {"result": []}}


def query_prometheus_instant(query: str) -> dict:
    """Query Prometheus instant API."""
    params = urllib.parse.urlencode({"query": query})
    url = f"http://localhost:{PROMETHEUS_PORT}/api/v1/query?{params}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"  Prometheus query failed: {e}", file=sys.stderr)
        return {"status": "error", "error": str(e), "data": {"result": []}}


def collect_infra_metrics(start_ts: float, end_ts: float, namespace: str = NAMESPACE) -> dict:
    """Collect infrastructure metrics from Prometheus for a time window (per-phase, 15s step)."""
    metrics = {}
    for name, query_tpl in INFRA_QUERIES.items():
        query = query_tpl.format(ns=namespace)
        result = query_prometheus_range(query, start_ts, end_ts)
        if result.get("status") == "success":
            metrics[name] = result["data"]["result"]
        else:
            metrics[name] = []
    return metrics


def collect_run_timeseries(window_start: float, window_end: float,
                            namespace: str = namespace_for_slot(CHAOS_SLOT),
                            step: str = TIMESERIES_STEP) -> dict:
    """Range-query every metric in TIMESERIES_QUERIES over the run window.

    Default step is 5 s. Never raises: a failed query is recorded with an
    empty result and an "error" key, and the other queries still run.
    """
    series = {}
    for name, tpl in TIMESERIES_QUERIES.items():
        query = tpl.format(ns=namespace)
        try:
            result = query_prometheus_range(query, window_start, window_end, step=step)
            if result.get("status") == "success":
                series[name] = {"query": query, "result": result["data"]["result"]}
            else:
                series[name] = {"query": query, "result": [], "error": result.get("error", "query failed")}
        except Exception as e:
            print(f"  WARNING: timeseries query '{name}' failed: {e}", file=sys.stderr)
            series[name] = {"query": query, "result": [], "error": str(e)}
    return series


def write_timeseries_sidecar(output_file: Path, window_start: float, window_end: float,
                              fault_start: float | None, fault_end: float | None,
                              prom_available: bool, namespace: str = namespace_for_slot(CHAOS_SLOT),
                              step: str = TIMESERIES_STEP) -> Path | None:
    """Write the gzipped Prometheus timeseries sidecar next to a run's JSON.

    For example run-3.json gets run-3.timeseries.json.gz. Any problem is
    logged and gives a skipped or partial sidecar, never a failed run.
    """
    if not prom_available:
        print("  WARNING: Prometheus unavailable, skipping timeseries sidecar.", file=sys.stderr)
        return None

    sidecar_path = output_file.parent / f"{output_file.stem}.timeseries.json.gz"

    # Some callers run this after cleanup_port_forwards(), so reopen the
    # Prometheus tunnel if it is not reachable.
    own_port_forward = False
    try:
        if query_prometheus_instant("up").get("status") != "success":
            raise RuntimeError("prometheus not reachable")
    except Exception:
        try:
            start_port_forward("monitoring", "prometheus-kube-prometheus-prometheus",
                               PROMETHEUS_PORT, 9090)
            own_port_forward = True
        except Exception as e:
            print(f"  WARNING: could not re-establish Prometheus port-forward for sidecar: {e}",
                  file=sys.stderr)
            return None

    try:
        metrics = collect_run_timeseries(window_start, window_end, namespace=namespace, step=step)
    except Exception as e:
        print(f"  WARNING: timeseries collection failed entirely: {e}", file=sys.stderr)
        return None
    finally:
        if own_port_forward:
            cleanup_port_forwards()

    payload = {
        "metadata": {
            "window_start": window_start,
            "window_end": window_end,
            "fault_start": fault_start,
            "fault_end": fault_end,
            "step": step,
            "namespace": namespace,
            # Exact queries used, so analysis can tell whether a sidecar
            # predates a query fix.
            "queries": {k: v.format(ns=namespace) for k, v in TIMESERIES_QUERIES.items()},
        },
        "metrics": metrics,
    }
    try:
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(sidecar_path, "wt", encoding="utf-8") as f:
            json.dump(payload, f, default=str)
        print(f"  Timeseries sidecar saved to: {sidecar_path}")
        return sidecar_path
    except Exception as e:
        print(f"  WARNING: failed to write timeseries sidecar: {e}", file=sys.stderr)
        return None

################################################################################
# wrk2 Job Management
################################################################################

def wrk2_job_name(label: str, run: int, slot: str | None = None) -> str:
    """Name for a wrk2 load-generator Job.

    slot defaults to CHAOS_SLOT. Slot 0 or unset gives wrk2-{label}-run{run};
    other slots add the slot number so concurrent slots never share a name.
    """
    if slot is None:
        slot = CHAOS_SLOT
    if not slot or slot == "0":
        return f"wrk2-{label}-run{run}"
    return f"wrk2-{slot}-{label}-run{run}"


def render_wrk2_job(label: str, run: int, duration: int,
                     namespace: str = namespace_for_slot(CHAOS_SLOT)) -> str:
    """Render the wrk2 Job YAML from the template.

    namespace sets both where the Job runs and which nginx-thrift Service it
    targets, and defaults to the CHAOS_SLOT namespace. The Job name always
    follows CHAOS_SLOT, so an explicit namespace should match it.
    """
    with open(WRK2_TEMPLATE) as f:
        template = f.read()

    job_name = wrk2_job_name(label, run)
    substitutions = {
        "JOB_NAME": job_name,
        "NAMESPACE": namespace,
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
                # wrk2 switches to m (minutes) and h under heavy queueing.
                m = re.search(r'([\d.]+)(us|ms|s|m|h)\b', line)
                if m:
                    val = float(m.group(1))
                    unit = m.group(2)
                    if unit == "us":
                        val /= 1000.0
                    elif unit == "s":
                        val *= 1000.0
                    elif unit == "m":
                        val *= 60000.0
                    elif unit == "h":
                        val *= 3600000.0
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
# Protocol Executors
################################################################################

# Warm-up after a state reset. Recreated stores start with cold caches, empty
# connection pools and index builds, and without an excluded warm-up wrk2's
# corrected latency absorbs minutes of cold-start queueing.
WARMUP_DURATION = int(os.environ.get("CHAOS_WARMUP_S", "120"))


def run_warmup(label: str, run_number: int,
               namespace: str = namespace_for_slot(CHAOS_SLOT)) -> float | None:
    """Drive wrk2 load for WARMUP_DURATION seconds and discard the results.

    Nothing is parsed or saved. Returns the time the warm-up Job was cleaned
    up, which callers use as the start of the sidecar window so it holds no
    warm-up traffic. Returns None when CHAOS_WARMUP_S=0. Component 1's
    sidecars were collected with an earlier fixed 60 s pre-window that
    overlaps the end of warm-up (analysis/PREREGISTRATION.md, Component 1
    amendment 5).
    """
    if WARMUP_DURATION <= 0:
        return None
    warm_label = f"warmup-{label}"
    job_name = wrk2_job_name(warm_label, run_number)
    print(f"\n  [Phase] WARMUP ({WARMUP_DURATION}s) - excluded from measurement...")
    cleanup_wrk2_job(job_name, namespace)
    time.sleep(2)
    try:
        kubectl_apply_stdin(render_wrk2_job(warm_label, run_number, WARMUP_DURATION, namespace=namespace))
        time.sleep(10)
        time.sleep(WARMUP_DURATION)
        time.sleep(5)
    finally:
        cleanup_wrk2_job(job_name, namespace)
    return time.time()


def run_flat_load(label: str, run_number: int, duration_s: int, prom_available: bool,
                   namespace: str = namespace_for_slot(CHAOS_SLOT)) -> dict:
    """Run wrk2 load for duration_s with no fault (overhead baseline and idle configs).

    Returns {"phases": {"load": ...}, "wrk2": ..., "_window": ...}. The caller
    passes _window to write_timeseries_sidecar and drops it before saving the
    run JSON. namespace defaults to the CHAOS_SLOT namespace.
    """
    warmup_end = run_warmup(label, run_number, namespace)

    job_name = wrk2_job_name(label, run_number)
    wrk2_yaml = render_wrk2_job(label, run_number, duration_s, namespace=namespace)

    cleanup_wrk2_job(job_name, namespace)
    time.sleep(2)

    result = {"phases": {}, "wrk2": {}}
    try:
        print(f"\n  [Phase] LOAD ({duration_s}s) - Starting wrk2 load, no fault...")
        kubectl_apply_stdin(wrk2_yaml)
        time.sleep(10)  # wait for pod scheduling

        load_start = time.time()
        time.sleep(duration_s)
        load_end = time.time()

        result["phases"]["load"] = {
            "start": load_start,
            "end": load_end,
            "infra_metrics": collect_infra_metrics(load_start, load_end, namespace) if prom_available else {},
        }

        print("\n  Collecting wrk2 results...")
        wait_for_job(job_name, namespace, timeout=120)
        wrk2_logs = kubectl_logs(job_name, namespace)
        result["wrk2"] = parse_wrk2_output(wrk2_logs)
        result["wrk2"]["raw_output"] = wrk2_logs[-5000:] if len(wrk2_logs) > 5000 else wrk2_logs

        result["_window"] = {
            # End of warm-up, or 60 s before the load when warm-up is off.
            "start": warmup_end if warmup_end is not None else load_start - 60,
            "end": load_end,
            "fault_start": None,
            "fault_end": None,
        }
    finally:
        cleanup_wrk2_job(job_name, namespace)

    return result


def render_experiment_manifest(experiment_path: Path, namespace: str) -> Path:
    """Return a fault manifest path that targets namespace.

    The 24 static manifests under experiments/{chaos-mesh,litmus}/ hardcode
    "social-network" in metadata.namespace and in the pod selector
    (selector.namespaces for Chaos Mesh, spec.appinfo.appns for LitmusChaos).
    kubectl -n overrides neither, so both are rewritten.

    For the default namespace the original path is returned. Otherwise a
    substituted copy is written to a temp directory and its path returned;
    the caller removes it. A manifest that already targets namespace, such as
    one from run-campaign.py's render_manifest, is returned unchanged,
    because substituting again would turn "social-network-2" into
    "social-network-2-2".
    """
    if namespace == NAMESPACE:
        return experiment_path
    text = experiment_path.read_text()
    if f"namespace: {namespace}" in text or f"appns: {namespace}" in text:
        return experiment_path
    rendered = text.replace(NAMESPACE, namespace)
    tmp_dir = Path(tempfile.mkdtemp())
    rendered_path = tmp_dir / experiment_path.name
    rendered_path.write_text(rendered)
    return rendered_path


def run_fault_protocol(experiment_path: Path, label: str, run_number: int,
                        baseline_s: int, fault_s: int, recovery_s: int, cooldown_s: int,
                        prom_available: bool, post_fault_restart: str | None = None,
                        namespace: str = namespace_for_slot(CHAOS_SLOT)) -> dict:
    """Run warm-up, then baseline, fault, recovery and optional cooldown under wrk2 load.

    One wrk2 Job runs for baseline_s + fault_s + recovery_s. --mode benchmark
    and the overhead fault config pass the four-phase durations;
    run-campaign.py passes the CAMPAIGN_* durations with cooldown_s=0, which
    skips the cooldown.

    Returns {"phases", "wrk2", "derived", "_window"}. _window holds the
    sidecar window (end of warm-up, or 60 s before baseline when warm-up is
    off, through the last phase) and the fault start and end. The caller
    passes it to write_timeseries_sidecar and drops it before saving the run
    JSON.

    Raises RuntimeError if the fault manifest cannot be applied. The caller
    manages the Prometheus port-forward. The wrk2 Job and fault manifest are
    always cleaned up, so repeated calls are safe. namespace defaults to the
    CHAOS_SLOT namespace.
    """
    warmup_end = run_warmup(label, run_number, namespace)

    job_name = wrk2_job_name(label, run_number)
    total_load_duration = baseline_s + fault_s + recovery_s
    wrk2_yaml = render_wrk2_job(label, run_number, total_load_duration, namespace=namespace)

    # Per-slot copy of the manifest; same path at the default namespace.
    manifest_path = render_experiment_manifest(experiment_path, namespace)

    cleanup_wrk2_job(job_name, namespace)
    kubectl_delete_file(str(manifest_path))
    time.sleep(5)

    results = {"phases": {}, "wrk2": {}, "derived": {}}

    try:
        # Phase 1: Start wrk2 load generator (runs for baseline+fault+recovery)
        print(f"\n  [Phase 1] BASELINE ({baseline_s}s) - Starting wrk2 load...")
        kubectl_apply_stdin(wrk2_yaml)
        time.sleep(10)  # wait for pod scheduling

        baseline_start = time.time()
        print(f"    Baseline started at {time.strftime('%H:%M:%S')}")
        time.sleep(baseline_s)
        baseline_end = time.time()

        results["phases"]["baseline"] = {
            "start": baseline_start,
            "end": baseline_end,
            "infra_metrics": collect_infra_metrics(baseline_start, baseline_end, namespace) if prom_available else {},
        }
        print(f"    Baseline complete at {time.strftime('%H:%M:%S')}")

        # Phase 2: Inject fault
        print(f"\n  [Phase 2] FAULT ({fault_s}s) - Injecting {label}...")
        fault_start = time.time()
        # Raise on failure. Continuing would record a fault window with no
        # fault active.
        if not kubectl_apply_file(str(manifest_path)):
            raise RuntimeError(f"fault manifest apply failed for {label} (namespace={namespace})")
        print(f"    Fault injected at {time.strftime('%H:%M:%S')}")
        time.sleep(fault_s)
        fault_end = time.time()

        results["phases"]["fault"] = {
            "start": fault_start,
            "end": fault_end,
            "infra_metrics": collect_infra_metrics(fault_start, fault_end, namespace) if prom_available else {},
        }
        print(f"    Fault phase complete at {time.strftime('%H:%M:%S')}")

        # Phase 3: Recovery
        print(f"\n  [Phase 3] RECOVERY ({recovery_s}s) - Removing fault...")
        recovery_start = time.time()
        kubectl_delete_file(str(manifest_path))
        print(f"    Fault removed at {time.strftime('%H:%M:%S')}")

        # HTTPChaos experiments leave stale iptables rules in the target
        # pod's network namespace. Restart the target deployment to clear them.
        if post_fault_restart:
            print(f"    Restarting {post_fault_restart} to clear stale network rules...")
            run_cmd(["kubectl", "rollout", "restart", f"deployment/{post_fault_restart}",
                     "-n", namespace], timeout=30)
            run_cmd(["kubectl", "rollout", "status", f"deployment/{post_fault_restart}",
                     "-n", namespace, "--timeout=60s"], timeout=90)
        time.sleep(recovery_s)
        recovery_end = time.time()

        results["phases"]["recovery"] = {
            "start": recovery_start,
            "end": recovery_end,
            "infra_metrics": collect_infra_metrics(recovery_start, recovery_end, namespace) if prom_available else {},
        }
        print(f"    Recovery complete at {time.strftime('%H:%M:%S')}")

        window_end = recovery_end

        # Phase 4: Cooldown, no metrics. Skipped when cooldown_s == 0.
        if cooldown_s > 0:
            print(f"\n  [Phase 4] COOLDOWN ({cooldown_s}s)...")
            time.sleep(cooldown_s)
            window_end = time.time()
            print(f"    Cooldown complete at {time.strftime('%H:%M:%S')}")

        # Collect wrk2 output
        print("\n  Collecting wrk2 results...")
        wait_for_job(job_name, namespace, timeout=120)
        wrk2_logs = kubectl_logs(job_name, namespace)
        results["wrk2"] = parse_wrk2_output(wrk2_logs)
        results["wrk2"]["raw_output"] = wrk2_logs[-5000:] if len(wrk2_logs) > 5000 else wrk2_logs

        # Compute derived metrics
        results["derived"] = compute_derived_metrics(results["phases"])

        results["_window"] = {
            # End of warm-up, or 60 s before baseline when warm-up is off.
            "start": warmup_end if warmup_end is not None else baseline_start - 60,
            "end": window_end,
            "fault_start": fault_start,
            "fault_end": fault_end,
        }
    finally:
        # Always clean up, also when an exception or KeyboardInterrupt
        # propagates to the caller.
        cleanup_wrk2_job(job_name, namespace)
        kubectl_delete_file(str(manifest_path))
        if manifest_path != experiment_path:
            shutil.rmtree(manifest_path.parent, ignore_errors=True)

    return results
