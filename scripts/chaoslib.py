#!/usr/bin/env python3
"""
Chaos Benchmark shared library.

Holds everything run-experiment.py, run-campaign.py, and run-overhead.sh's
Python entry points need in common: kubectl/subprocess helpers, port-forward
management, Prometheus queries (including full-run-window timeseries capture
for the ML analysis), wrk2 job rendering/parsing, derived-metric computation,
and the two protocol executors (`run_fault_protocol` for the 3/4-phase
baseline->fault->[recovery->cooldown] sequence, `run_flat_load` for a single
undisturbed load window used by overhead-isolation baseline/idle configs).

This module exists mainly because run-experiment.py's filename has a hyphen
and cannot be `import`-ed directly by run-campaign.py; pulling the reusable
pieces out here avoids importlib.util.spec_from_file_location tricks and
keeps run-experiment.py's CLI behavior byte-compatible with what it was
before this refactor.
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

################################################################################
# Configuration
################################################################################

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXPERIMENTS_DIR = PROJECT_ROOT / "experiments"
# CHAOS_DATA_DIR isolates the revision campaign's results from the archived
# original 120-run dataset in data/ (different region/capacity; mixing them
# via resume would reintroduce the infrastructure confound). Per-cluster roots:
# data-v2/bench-a, data-v2/bench-b, data-v2/ml.
DATA_DIR = Path(os.environ.get("CHAOS_DATA_DIR", str(PROJECT_ROOT / "data")))
WRK2_TEMPLATE = PROJECT_ROOT / "load-generator" / "wrk2-job.yaml.tpl"

# Overridden per-account: export CHAOS_ECR_REPO after build-wrk2-image.sh
# pushes to the target account/region (default = original us-east-1 image).
ECR_REPO = os.environ.get(
    "CHAOS_ECR_REPO",
    "886604922358.dkr.ecr.us-east-1.amazonaws.com/chaos-benchmark/wrk2",
)
NAMESPACE = "social-network"

# Slot parallelism (backward-compatible extension): a cluster can run up to
# 3 concurrent, fully isolated copies of the DeathStarBench stack, one per
# namespace, each pinned to its own 3 dedicated nodes (node label
# chaos-slot=0/1/2). Slot 0 (or CHAOS_SLOT unset) is the pre-existing
# single-namespace behavior, byte-for-byte -- this is load-bearing, since a
# campaign invoked without CHAOS_SLOT must be completely unaffected by any
# of this. See scripts/SLOT_PARALLELISM.md.
CHAOS_SLOT = os.environ.get("CHAOS_SLOT", "0")


def namespace_for_slot(slot: str | None) -> str:
    """Namespace convention for slot parallelism: slot 0/unset -> the
    existing default namespace (backward compat); slot N (N != "0") ->
    "<NAMESPACE>-<N>". Kept in sync with reset-app-state.sh and
    init-social-graph.sh's bash re-implementation of this same logic -- if
    you change it here, change it in both places (each has a cross-reference
    comment back to this function)."""
    return NAMESPACE if not slot or slot == "0" else f"{NAMESPACE}-{slot}"


# Small deterministic per-slot port offset, stacked on top of the existing
# per-cluster KUBECONFIG-checksum offset used by reset-app-state.sh and
# init-social-graph.sh for their local verification/init port-forwards. That
# checksum offset alone only separates *clusters* (different KUBECONFIG ->
# different checksum); it does nothing for 3 slots sharing one KUBECONFIG on
# the same cluster, which is the new case this offset covers. 97 is
# arbitrary but must stay in sync with the bash scripts' literal `* 97`
# (each has a cross-reference comment back to this constant).
SLOT_PORT_STEP = 97


def slot_port_offset(slot: str | None, base: int) -> int:
    """Slot-specific local port, so 3 slots on one KUBECONFIG never collide
    on a port-forward. Slot 0/unset returns `base` unchanged (backward
    compat). Not needed for PROMETHEUS_PORT -- all slots query the same
    shared Prometheus instance in the monitoring namespace via a different
    `namespace=` argument to the query functions, not a different port."""
    if not slot or slot == "0":
        return base
    return base + int(slot) * SLOT_PORT_STEP


# Original 4-phase protocol timing (seconds). --mode benchmark uses these
# unmodified so the original 120-run (now 720-run) dataset stays reproducible.
BASELINE_DURATION = 300
FAULT_DURATION = 120
RECOVERY_DURATION = 60
COOLDOWN_DURATION = 60
TOTAL_LOAD_DURATION = BASELINE_DURATION + FAULT_DURATION + RECOVERY_DURATION

# Overhead-isolation mode: flat 300s load window, no fault.
OVERHEAD_LOAD_DURATION = 300

# Campaign mode: shortened per-injection protocol, no cooldown.
CAMPAIGN_BASELINE_DURATION = 120
CAMPAIGN_FAULT_DURATION = 120
CAMPAIGN_RECOVERY_DURATION = 60

# Load generator defaults
WRK_THREADS = "4"
WRK_CONNECTIONS = "100"
# Offered load. The original 200 rps SATURATES the SUT (fresh-state capacity
# ~160-180 rps), so wrk2's corrected latency measured queueing collapse, not
# fault impact (pilot: p99 0.98 minutes on a clean baseline). The campaign
# runs at 120 rps (~2/3 of measured capacity) so latency is interpretable.
WRK_RATE = os.environ.get("CHAOS_LOAD_RPS", "120")

# Prometheus
# Per-process override so two clusters' runners can port-forward concurrently
# on one workstation (e.g. bench-a=9090, bench-b=9091).
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
    # No container!=""/!="POD" filter here: container_network_*_total is a
    # cAdvisor POD-LEVEL metric (network namespaces are shared per-pod, not
    # per-container) and carries no "container" label at all, so that filter
    # (correct for the per-container cpu/memory/restarts queries below)
    # excludes every series and silently zeroed this metric in every sidecar
    # since the filter was added. Confirmed live: 0 series with the filter,
    # 28 without, on a real run. [2m] window for the same scrape-alignment
    # reason as node_cpu_utilization below (confirmed live: [30s]=14 series,
    # [2m]=28 series).
    "network_rx_bytes": 'sum(rate(container_network_receive_bytes_total{{namespace="{ns}"}}[2m])) by (pod)',
}

# Full-run-window queries for the per-run timeseries sidecar (ML analysis
# input). These widen INFRA_QUERIES to range queries plus node-level and
# best-effort per-service L7 queries. A query returning no series is not an
# error: DeathStarBench's stock nginx-thrift/Thrift services do not export
# Envoy/Istio-style L7 metrics unless the cluster has a mesh installed, so
# http_* entries are expected to be empty on an un-meshed cluster.
TIMESERIES_QUERIES = {
    "container_cpu_usage": 'sum(rate(container_cpu_usage_seconds_total{{namespace="{ns}", container!="", container!="POD"}}[30s])) by (pod)',
    "container_memory_working_set": 'sum(container_memory_working_set_bytes{{namespace="{ns}", container!="", container!="POD"}}) by (pod)',
    # See INFRA_QUERIES["network_rx_bytes"] above for why no container filter
    # and a [2m] window: confirmed live, the container!=""/!="POD" filter
    # excludes 100% of series for this pod-level metric.
    "container_network_rx_bytes": 'sum(rate(container_network_receive_bytes_total{{namespace="{ns}"}}[2m])) by (pod)',
    "container_network_tx_bytes": 'sum(rate(container_network_transmit_bytes_total{{namespace="{ns}"}}[2m])) by (pod)',
    "pod_restarts_total": 'sum(kube_pod_container_status_restarts_total{{namespace="{ns}", container!="", container!="POD"}}) by (pod)',
    # node-exporter scrapes every 30s, so a [30s] rate window rarely holds the
    # two samples rate() needs and returns empty; [2m] is required here.
    "node_cpu_utilization": 'sum(rate(node_cpu_seconds_total{{mode!="idle"}}[2m])) by (instance) / count(node_cpu_seconds_total{{mode="idle"}}) by (instance)',
    "node_memory_used_bytes": "node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes",
    # Best-effort per-service L7 metrics (see docstring above).
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
                            namespace: str = NAMESPACE, step: str = TIMESERIES_STEP) -> dict:
    """Best-effort full-run-window range query for every metric in
    TIMESERIES_QUERIES, at fine (default 5s) resolution, for the ML sidecar.

    Never raises. A failing or empty query is recorded with an "error" key
    (or an empty result list) rather than aborting the whole collection, so
    one bad query never costs the rest of the timeseries.
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
                              prom_available: bool, namespace: str = NAMESPACE,
                              step: str = TIMESERIES_STEP) -> Path | None:
    """Write the gzipped raw-Prometheus-timeseries sidecar next to a run's
    output JSON (e.g. run-3.json -> run-3.timeseries.json.gz). Failure
    tolerant throughout: any problem is logged and results in a skipped or
    partial sidecar, never a failed run.
    """
    if not prom_available:
        print("  WARNING: Prometheus unavailable, skipping timeseries sidecar.", file=sys.stderr)
        return None

    sidecar_path = output_file.parent / f"{output_file.stem}.timeseries.json.gz"

    # Callers invoke this AFTER cleanup_port_forwards() (their finally block),
    # so the Prometheus tunnel is usually gone by now; without this re-check
    # every range query fails with connection-refused and the sidecar is junk.
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
            # Provenance: exact queries used, so analysis can detect if a
            # sidecar predates a query fix.
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
    """Job name for a wrk2 load-generator Job. `slot` defaults to the
    module-level CHAOS_SLOT (env-driven). Slot 0/unset keeps the original
    wrk2-{label}-run{run} pattern byte-for-byte (backward compat for the
    currently-running default-namespace campaign); any other slot prefixes
    the slot so concurrent slots on one cluster never collide on Job name."""
    if slot is None:
        slot = CHAOS_SLOT
    if not slot or slot == "0":
        return f"wrk2-{label}-run{run}"
    return f"wrk2-{slot}-{label}-run{run}"


def render_wrk2_job(label: str, run: int, duration: int,
                     namespace: str = namespace_for_slot(CHAOS_SLOT)) -> str:
    """Render wrk2 Job YAML from template. `namespace` controls both where
    the Job is created and which nginx-thrift Service it targets (the
    template substitutes both from the same NAMESPACE placeholder).
    Defaults to namespace_for_slot(CHAOS_SLOT), so a CHAOS_SLOT-unset
    invocation renders exactly what it rendered before slot support
    existed. Job name is derived from CHAOS_SLOT (via wrk2_job_name), not
    from `namespace` -- if a caller passes an explicit `namespace` that
    doesn't correspond to CHAOS_SLOT, the Job name won't reflect it; slot
    and namespace are meant to be driven together via CHAOS_SLOT."""
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
                # wrk2 prints m (minutes) and h under extreme queueing; a
                # parser that only knows us/ms/s silently records 0
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

# Warm-up after a state reset: freshly recreated stores mean cold caches,
# empty connection pools, and index builds; without an excluded warm-up
# window, wrk2's corrected latency histogram absorbs minutes-scale cold-start
# queueing (pilot: p99 of 1.96 MINUTES on the first post-reset run).
WARMUP_DURATION = int(os.environ.get("CHAOS_WARMUP_S", "120"))


def run_warmup(label: str, run_number: int,
               namespace: str = namespace_for_slot(CHAOS_SLOT)) -> float | None:
    """Drive wrk2 load for WARMUP_DURATION seconds and discard the results.
    Not a recorded phase: nothing is parsed or persisted. No-op (returns
    None) when CHAOS_WARMUP_S=0.

    Returns the wall-clock time this function returns (i.e. warmup traffic
    fully stopped and its Job is cleaned up). Callers use this as the
    sidecar's pre-window start instead of a hardcoded "-60s before
    baseline_start" -- that fixed offset assumed >=60s would elapse between
    warmup ending and baseline_start, but the actual gap is ~15-20s (a few
    kubectl round-trips + a short settle sleep), so -60s reached back into
    the tail of the warmup job's own traffic for every run collected before
    this fix (audit finding, Aug 15; confirmed via real run timestamps: the
    60s window consistently overlapped ~40s of live warmup load). Threading
    the true end-of-warmup timestamp through eliminates the overlap
    structurally instead of guessing a safe buffer.
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
    """Run wrk2 load for duration_s with no fault injection at all. Used by
    overhead-isolation baseline/idle configs. Returns a dict with a single
    "load" phase plus "_window" timing metadata (consumed by the caller to
    drive write_timeseries_sidecar, then discarded before the run JSON is
    written).

    `namespace` defaults to namespace_for_slot(CHAOS_SLOT): callers that
    don't pass it explicitly automatically get slot-aware behavior from the
    CHAOS_SLOT env var (unset/"0" -> unchanged default-namespace behavior).
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
            # Use the real warmup-end timestamp when warmup ran (eliminates
            # the overlap; see run_warmup's docstring), else fall back to
            # the original -60s heuristic when warmup is disabled.
            "start": warmup_end if warmup_end is not None else load_start - 60,
            "end": load_end,
            "fault_start": None,
            "fault_end": None,
        }
    finally:
        cleanup_wrk2_job(job_name, namespace)

    return result


def render_experiment_manifest(experiment_path: Path, namespace: str) -> Path:
    """Return a fault-manifest path targeting `namespace`.

    The 24 static YAMLs under experiments/{chaos-mesh,litmus}/ hardcode
    "social-network" in both metadata.namespace and the pod-selector field
    (selector.namespaces for Chaos Mesh, spec.appinfo.appns for Litmus) --
    kubectl's -n flag does not override metadata.namespace, and the selector
    field controls fault TARGETING independently, so both must change for a
    slot to actually fault-inject into its own namespace instead of slot 0's.

    When namespace is the default ("social-network"), returns experiment_path
    unchanged -- byte-for-byte original behavior, no temp file, no rewrite.
    Otherwise renders a substituted copy to a temp file and returns that path;
    caller is responsible for cleanup (not done here, since the same
    experiment_path is applied and deleted multiple times across one
    run_fault_protocol call and cleanup must happen once, at the end).
    """
    if namespace == NAMESPACE:
        return experiment_path
    text = experiment_path.read_text()
    rendered = text.replace(NAMESPACE, namespace)
    tmp_dir = Path(tempfile.mkdtemp())
    rendered_path = tmp_dir / experiment_path.name
    rendered_path.write_text(rendered)
    return rendered_path


def run_fault_protocol(experiment_path: Path, label: str, run_number: int,
                        baseline_s: int, fault_s: int, recovery_s: int, cooldown_s: int,
                        prom_available: bool, post_fault_restart: str | None = None,
                        namespace: str = namespace_for_slot(CHAOS_SLOT)) -> dict:
    """Execute baseline -> fault -> recovery -> [cooldown] against a wrk2
    load generator running for baseline_s+fault_s+recovery_s seconds.

    Used by --mode benchmark (durations = BASELINE/FAULT/RECOVERY/COOLDOWN_DURATION,
    cooldown_s > 0), --mode overhead's "fault" config (same durations), and
    run-campaign.py (CAMPAIGN_*_DURATION, cooldown_s = 0 -> cooldown phase
    skipped entirely).

    Returns {"phases", "wrk2", "derived", "_window"}. "_window" carries the
    full-run timing (baseline_start - 60s through the last phase's end, plus
    exact fault start/end) for the caller to hand to write_timeseries_sidecar;
    callers should pop it before persisting the run's own JSON so that
    schema stays exactly what it was pre-refactor.

    Does not manage the Prometheus port-forward (caller's responsibility,
    since a campaign wants one port-forward across all 10 injections rather
    than one per injection). Does manage wrk2 Job and fault-manifest cleanup
    per invocation via its own finally block, so it is safe to call this
    repeatedly against the same cluster.

    `namespace` defaults to namespace_for_slot(CHAOS_SLOT): callers that
    don't pass it explicitly automatically get slot-aware behavior from the
    CHAOS_SLOT env var (unset/"0" -> unchanged default-namespace behavior).
    Explicit namespace= callers are unaffected by CHAOS_SLOT.
    """
    warmup_end = run_warmup(label, run_number, namespace)

    job_name = wrk2_job_name(label, run_number)
    total_load_duration = baseline_s + fault_s + recovery_s
    wrk2_yaml = render_wrk2_job(label, run_number, total_load_duration, namespace=namespace)

    # Fault manifests hardcode namespace: social-network in both
    # metadata.namespace and the pod selector -- render a per-slot copy so
    # slot!=0 actually targets its own namespace. No-op (same path) at the
    # default namespace. See render_experiment_manifest docstring.
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
        kubectl_apply_file(str(manifest_path))
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

        # Phase 4: Cooldown (no metrics collection, just wait). Skipped
        # entirely when cooldown_s == 0 (campaign mode).
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
            # See run_flat_load's identical fix + run_warmup's docstring.
            "start": warmup_end if warmup_end is not None else baseline_start - 60,
            "end": window_end,
            "fault_start": fault_start,
            "fault_end": fault_end,
        }
    finally:
        # Cleanup (always, even on exception/KeyboardInterrupt propagating
        # to the caller -- caller decides whether to catch and record it).
        cleanup_wrk2_job(job_name, namespace)
        kubectl_delete_file(str(manifest_path))
        if manifest_path != experiment_path:
            shutil.rmtree(manifest_path.parent, ignore_errors=True)

    return results
