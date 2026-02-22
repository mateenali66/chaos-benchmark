#!/usr/bin/env python3
"""
Phase 5: Statistical analysis of chaos engineering benchmark data.

Aggregates 120 experiment JSON files (2 tools x 12 scenarios x 5 reps),
computes summary statistics, runs statistical tests, and exports results
for paper tables and figures.

Usage:
    python analysis/analyze.py

Outputs:
    analysis/results/summary_stats.csv
    analysis/results/scenario_comparison.csv
    analysis/results/statistical_tests.csv
    analysis/results/overhead_comparison.csv
    analysis/results/category_comparison.csv
"""

import json
import glob
import os
import csv
import math
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATA_DIR = Path(__file__).parent.parent / "data"
OUTPUT_DIR = Path(__file__).parent / "results"
OUTPUT_DIR.mkdir(exist_ok=True)

SCENARIO_NAMES = {
    "p1": "Pod Kill",
    "p2": "Container Kill",
    "p3": "Pod Failure",
    "n1": "Latency 50ms",
    "n2": "Latency 100ms",
    "n3": "Latency 300ms",
    "n4": "Packet Loss 5%",
    "n5": "Network Partition",
    "r1": "CPU Stress 80%",
    "r2": "Memory Pressure 80%",
    "a1": "HTTP Abort 503",
    "a2": "gRPC Unavailable",
}

SCENARIO_CATEGORIES = {
    "p1": "Pod/Container",
    "p2": "Pod/Container",
    "p3": "Pod/Container",
    "n1": "Network",
    "n2": "Network",
    "n3": "Network",
    "n4": "Network",
    "n5": "Network",
    "r1": "Resource",
    "r2": "Resource",
    "a1": "Application",
    "a2": "Application",
}

TOOL_LABELS = {"chaos-mesh": "Chaos Mesh", "litmus": "LitmusChaos"}


# ---------------------------------------------------------------------------
# Statistical helpers (stdlib only -- no scipy/numpy dependency for CI)
# ---------------------------------------------------------------------------

def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def median(xs):
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def stdev(xs):
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def ci95(xs):
    """95% confidence interval (t-distribution approximation for n=5)."""
    if len(xs) < 2:
        return 0.0
    # t critical value for 95% CI, df=4 (n-1 for n=5): 2.776
    t_crit = {2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 9: 2.262, 19: 2.093}
    df = len(xs) - 1
    t = t_crit.get(df, 2.0)  # fallback
    return t * stdev(xs) / math.sqrt(len(xs))


def wilcoxon_signed_rank(x, y):
    """
    Wilcoxon signed-rank test (exact, two-sided) for paired samples.
    Returns (W_statistic, approximate_p_value).
    For n=5 paired samples, uses the exact critical values.
    """
    diffs = [a - b for a, b in zip(x, y)]
    # Remove zeros
    diffs = [d for d in diffs if d != 0]
    n = len(diffs)
    if n == 0:
        return 0.0, 1.0

    # Rank absolute differences
    abs_diffs = [(abs(d), i) for i, d in enumerate(diffs)]
    abs_diffs.sort()
    ranks = [0.0] * n

    i = 0
    while i < n:
        j = i
        while j < n and abs_diffs[j][0] == abs_diffs[i][0]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[abs_diffs[k][1]] = avg_rank
        i = j

    W_plus = sum(r for r, d in zip(ranks, diffs) if d > 0)
    W_minus = sum(r for r, d in zip(ranks, diffs) if d < 0)
    W = min(W_plus, W_minus)

    # For small n, use exact critical values (two-sided, alpha=0.05)
    # If W <= critical value, reject null. p-value approximation:
    # For n=5: W_crit(0.05) = 1 (exact tables)
    # Normal approximation for p-value
    E_W = n * (n + 1) / 4.0
    Var_W = n * (n + 1) * (2 * n + 1) / 24.0
    if Var_W == 0:
        return W, 1.0
    z = (W - E_W) / math.sqrt(Var_W)
    # Two-sided p-value from standard normal (approximation)
    p = 2 * (1 - normal_cdf(abs(z)))
    return W, p


def normal_cdf(z):
    """Standard normal CDF approximation (Abramowitz & Stegun)."""
    if z < 0:
        return 1 - normal_cdf(-z)
    t = 1.0 / (1.0 + 0.2316419 * z)
    d = 0.3989422804014327  # 1/sqrt(2*pi)
    p = d * math.exp(-z * z / 2.0) * (
        t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 +
        t * (-1.821255978 + t * 1.330274429))))
    )
    return 1.0 - p


def cliffs_delta(x, y):
    """
    Cliff's delta effect size for two independent groups.
    Returns (delta, magnitude) where magnitude is negligible/small/medium/large.
    """
    n_x, n_y = len(x), len(y)
    if n_x == 0 or n_y == 0:
        return 0.0, "negligible"

    more = sum(1 for xi in x for yi in y if xi > yi)
    less = sum(1 for xi in x for yi in y if xi < yi)
    delta = (more - less) / (n_x * n_y)

    abs_d = abs(delta)
    if abs_d < 0.147:
        mag = "negligible"
    elif abs_d < 0.33:
        mag = "small"
    elif abs_d < 0.474:
        mag = "medium"
    else:
        mag = "large"

    return delta, mag


def bonferroni(p_values, alpha=0.05):
    """Apply Bonferroni correction to a list of p-values."""
    m = len(p_values)
    adjusted = [min(p * m, 1.0) for p in p_values]
    significant = [p < alpha for p in adjusted]
    return adjusted, significant


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_all_experiments():
    """Load all experiment JSON files into a list of dicts."""
    records = []
    for fpath in sorted(glob.glob(str(DATA_DIR / "*" / "*" / "run-*.json"))):
        with open(fpath) as f:
            d = json.load(f)

        m = d["metadata"]
        w = d["wrk2"]
        dr = d.get("derived", {})
        phases = d.get("phases", {})

        # Compute aggregate infra metrics per phase
        baseline_cpu = _aggregate_cpu(phases.get("baseline", {}))
        fault_cpu = _aggregate_cpu(phases.get("fault", {}))
        recovery_cpu = _aggregate_cpu(phases.get("recovery", {}))

        baseline_mem = _aggregate_memory(phases.get("baseline", {}))
        fault_mem = _aggregate_memory(phases.get("fault", {}))
        recovery_mem = _aggregate_memory(phases.get("recovery", {}))

        records.append({
            "tool": m["tool"],
            "scenario": m["scenario"],
            "scenario_name": SCENARIO_NAMES.get(m["scenario"], m["scenario"]),
            "category": SCENARIO_CATEGORIES.get(m["scenario"], "Unknown"),
            "run": m["run"],
            "file": fpath,
            # wrk2 metrics
            "throughput_rps": w["throughput_rps"],
            "latency_p50": w["latency_ms"]["p50"],
            "latency_p95": w["latency_ms"]["p95"],
            "latency_p99": w["latency_ms"]["p99"],
            "latency_mean": w["latency_ms"]["mean"],
            "errors_timeout": w["errors"]["timeout"],
            "errors_http": w["errors"]["http_non2xx3xx"],
            "errors_read": w["errors"]["read"],
            "errors_write": w["errors"]["write"],
            "requests_total": w["requests_total"],
            "error_rate": (
                (w["errors"]["timeout"] + w["errors"]["http_non2xx3xx"] +
                 w["errors"]["read"] + w["errors"]["write"]) /
                max(w["requests_total"], 1)
            ),
            # derived
            "pod_restarts": dr.get("pod_restarts_during_fault", 0),
            "cpu_spike_pct": dr.get("cpu_spike_pct", 0),
            "memory_spike_mb": dr.get("memory_spike_mb", 0),
            # phase-level aggregates
            "baseline_cpu_mean": baseline_cpu,
            "fault_cpu_mean": fault_cpu,
            "recovery_cpu_mean": recovery_cpu,
            "baseline_mem_mean": baseline_mem,
            "fault_mem_mean": fault_mem,
            "recovery_mem_mean": recovery_mem,
        })

    return records


def _aggregate_cpu(phase_data):
    """Compute mean CPU usage across all pods and time points in a phase."""
    infra = phase_data.get("infra_metrics", {})
    cpu_series = infra.get("cpu_usage", [])
    all_vals = []
    for pod_data in cpu_series:
        for ts, val in pod_data.get("values", []):
            try:
                v = float(val)
                if v > 0:
                    all_vals.append(v)
            except (ValueError, TypeError):
                pass
    return mean(all_vals) if all_vals else 0.0


def _aggregate_memory(phase_data):
    """Compute mean memory usage (MB) across all pods in a phase."""
    infra = phase_data.get("infra_metrics", {})
    mem_series = infra.get("memory_usage", [])
    all_vals = []
    for pod_data in mem_series:
        for ts, val in pod_data.get("values", []):
            try:
                v = float(val) / (1024 * 1024)  # bytes to MB
                if v > 0:
                    all_vals.append(v)
            except (ValueError, TypeError):
                pass
    return mean(all_vals) if all_vals else 0.0


# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------

def compute_summary_stats(records):
    """Compute per-tool per-scenario summary statistics."""
    groups = defaultdict(list)
    for r in records:
        key = (r["tool"], r["scenario"])
        groups[key].append(r)

    rows = []
    for (tool, scenario), recs in sorted(groups.items()):
        rps_vals = [r["throughput_rps"] for r in recs]
        p50_vals = [r["latency_p50"] for r in recs]
        p99_vals = [r["latency_p99"] for r in recs]
        mean_lat = [r["latency_mean"] for r in recs]
        error_rates = [r["error_rate"] for r in recs]
        restarts = [r["pod_restarts"] for r in recs]
        cpu_spikes = [r["cpu_spike_pct"] for r in recs]
        mem_spikes = [r["memory_spike_mb"] for r in recs]

        rows.append({
            "tool": TOOL_LABELS.get(tool, tool),
            "scenario": scenario,
            "scenario_name": SCENARIO_NAMES.get(scenario, scenario),
            "category": SCENARIO_CATEGORIES.get(scenario, ""),
            "n": len(recs),
            "rps_mean": round(mean(rps_vals), 2),
            "rps_median": round(median(rps_vals), 2),
            "rps_stdev": round(stdev(rps_vals), 2),
            "rps_ci95": round(ci95(rps_vals), 2),
            "p50_mean": round(mean(p50_vals), 2),
            "p99_mean": round(mean(p99_vals), 2),
            "latency_mean": round(mean(mean_lat), 2),
            "error_rate_mean": round(mean(error_rates), 4),
            "pod_restarts_mean": round(mean(restarts), 2),
            "cpu_spike_mean": round(mean(cpu_spikes), 2),
            "memory_spike_mean": round(mean(mem_spikes), 2),
        })

    return rows


def compute_pairwise_tests(records):
    """Run Wilcoxon signed-rank tests and Cliff's delta for each scenario."""
    groups = defaultdict(list)
    for r in records:
        key = (r["tool"], r["scenario"])
        groups[key].append(r)

    scenarios = sorted(set(r["scenario"] for r in records))
    p_values = []
    test_rows = []

    for scenario in scenarios:
        cm_recs = sorted(groups.get(("chaos-mesh", scenario), []), key=lambda r: r["run"])
        lt_recs = sorted(groups.get(("litmus", scenario), []), key=lambda r: r["run"])

        if len(cm_recs) != 5 or len(lt_recs) != 5:
            continue

        cm_rps = [r["throughput_rps"] for r in cm_recs]
        lt_rps = [r["throughput_rps"] for r in lt_recs]

        cm_p99 = [r["latency_p99"] for r in cm_recs]
        lt_p99 = [r["latency_p99"] for r in lt_recs]

        cm_err = [r["error_rate"] for r in cm_recs]
        lt_err = [r["error_rate"] for r in lt_recs]

        # Throughput comparison
        W_rps, p_rps = wilcoxon_signed_rank(cm_rps, lt_rps)
        delta_rps, mag_rps = cliffs_delta(cm_rps, lt_rps)
        p_values.append(p_rps)

        # Latency comparison
        W_lat, p_lat = wilcoxon_signed_rank(cm_p99, lt_p99)
        delta_lat, mag_lat = cliffs_delta(cm_p99, lt_p99)
        p_values.append(p_lat)

        # Error rate comparison
        W_err, p_err = wilcoxon_signed_rank(cm_err, lt_err)
        delta_err, mag_err = cliffs_delta(cm_err, lt_err)
        p_values.append(p_err)

        test_rows.append({
            "scenario": scenario,
            "scenario_name": SCENARIO_NAMES.get(scenario, scenario),
            "metric": "Throughput (rps)",
            "cm_mean": round(mean(cm_rps), 2),
            "lt_mean": round(mean(lt_rps), 2),
            "diff_pct": round((mean(cm_rps) - mean(lt_rps)) / max(mean(lt_rps), 0.01) * 100, 1),
            "W": round(W_rps, 2),
            "p_value": round(p_rps, 4),
            "cliffs_delta": round(delta_rps, 3),
            "effect_size": mag_rps,
        })
        test_rows.append({
            "scenario": scenario,
            "scenario_name": SCENARIO_NAMES.get(scenario, scenario),
            "metric": "Latency p99 (ms)",
            "cm_mean": round(mean(cm_p99), 2),
            "lt_mean": round(mean(lt_p99), 2),
            "diff_pct": round((mean(cm_p99) - mean(lt_p99)) / max(mean(lt_p99), 0.01) * 100, 1),
            "W": round(W_lat, 2),
            "p_value": round(p_lat, 4),
            "cliffs_delta": round(delta_lat, 3),
            "effect_size": mag_lat,
        })
        test_rows.append({
            "scenario": scenario,
            "scenario_name": SCENARIO_NAMES.get(scenario, scenario),
            "metric": "Error rate",
            "cm_mean": round(mean(cm_err), 4),
            "lt_mean": round(mean(lt_err), 4),
            "diff_pct": round((mean(cm_err) - mean(lt_err)) / max(mean(lt_err), 0.0001) * 100, 1),
            "W": round(W_err, 2),
            "p_value": round(p_err, 4),
            "cliffs_delta": round(delta_err, 3),
            "effect_size": mag_err,
        })

    # Apply Bonferroni correction
    if p_values:
        adjusted, significant = bonferroni(p_values)
        for i, row in enumerate(test_rows):
            row["p_adjusted"] = round(adjusted[i], 4)
            row["significant"] = significant[i]

    return test_rows


def compute_overhead(records):
    """Compute tool overhead: CPU and memory usage by chaos tool infrastructure."""
    groups = defaultdict(list)
    for r in records:
        key = (r["tool"], r["scenario"])
        groups[key].append(r)

    rows = []
    for (tool, scenario), recs in sorted(groups.items()):
        baseline_cpu = [r["baseline_cpu_mean"] for r in recs]
        fault_cpu = [r["fault_cpu_mean"] for r in recs]
        recovery_cpu = [r["recovery_cpu_mean"] for r in recs]

        baseline_mem = [r["baseline_mem_mean"] for r in recs]
        fault_mem = [r["fault_mem_mean"] for r in recs]
        recovery_mem = [r["recovery_mem_mean"] for r in recs]

        # CPU overhead = fault_cpu - baseline_cpu
        cpu_overhead = [f - b for f, b in zip(fault_cpu, baseline_cpu)]
        mem_overhead = [f - b for f, b in zip(fault_mem, baseline_mem)]

        rows.append({
            "tool": TOOL_LABELS.get(tool, tool),
            "scenario": scenario,
            "scenario_name": SCENARIO_NAMES.get(scenario, scenario),
            "category": SCENARIO_CATEGORIES.get(scenario, ""),
            "baseline_cpu_mean": round(mean(baseline_cpu), 6),
            "fault_cpu_mean": round(mean(fault_cpu), 6),
            "cpu_overhead": round(mean(cpu_overhead), 6),
            "cpu_overhead_pct": round(
                mean(cpu_overhead) / max(mean(baseline_cpu), 0.0001) * 100, 2
            ),
            "baseline_mem_mb": round(mean(baseline_mem), 2),
            "fault_mem_mb": round(mean(fault_mem), 2),
            "mem_overhead_mb": round(mean(mem_overhead), 2),
            "mem_overhead_pct": round(
                mean(mem_overhead) / max(mean(baseline_mem), 0.01) * 100, 2
            ),
        })

    return rows


def compute_category_comparison(records):
    """Aggregate results by fault category."""
    groups = defaultdict(list)
    for r in records:
        key = (r["tool"], r["category"])
        groups[key].append(r)

    rows = []
    for (tool, cat), recs in sorted(groups.items()):
        rps = [r["throughput_rps"] for r in recs]
        p99 = [r["latency_p99"] for r in recs]
        err = [r["error_rate"] for r in recs]
        restarts = [r["pod_restarts"] for r in recs]

        rows.append({
            "tool": TOOL_LABELS.get(tool, tool),
            "category": cat,
            "n_experiments": len(recs),
            "rps_mean": round(mean(rps), 2),
            "rps_stdev": round(stdev(rps), 2),
            "p99_mean": round(mean(p99), 2),
            "error_rate_mean": round(mean(err), 4),
            "pod_restarts_mean": round(mean(restarts), 2),
        })

    return rows


def compute_tool_summary(records):
    """Overall tool comparison."""
    rows = []
    for tool in ["chaos-mesh", "litmus"]:
        recs = [r for r in records if r["tool"] == tool]
        rps = [r["throughput_rps"] for r in recs]
        p99 = [r["latency_p99"] for r in recs]
        err = [r["error_rate"] for r in recs]
        restarts = [r["pod_restarts"] for r in recs]
        cpu = [r["cpu_spike_pct"] for r in recs]
        mem = [r["memory_spike_mb"] for r in recs]

        rows.append({
            "tool": TOOL_LABELS.get(tool, tool),
            "n": len(recs),
            "rps_mean": round(mean(rps), 2),
            "rps_median": round(median(rps), 2),
            "rps_stdev": round(stdev(rps), 2),
            "rps_ci95": round(ci95(rps), 2),
            "p99_mean": round(mean(p99), 2),
            "p99_stdev": round(stdev(p99), 2),
            "error_rate_mean": round(mean(err), 4),
            "pod_restarts_total": sum(restarts),
            "cpu_spike_mean": round(mean(cpu), 2),
            "mem_spike_mean": round(mean(mem), 2),
        })

    return rows


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

def write_csv(rows, filename):
    if not rows:
        return
    filepath = OUTPUT_DIR / filename
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Written: {filepath}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Loading experiment data...")
    records = load_all_experiments()
    print(f"  Loaded {len(records)} experiments")
    print()

    print("Computing summary statistics...")
    summary = compute_summary_stats(records)
    write_csv(summary, "summary_stats.csv")

    print("Computing tool-level summary...")
    tool_summary = compute_tool_summary(records)
    write_csv(tool_summary, "tool_summary.csv")

    print("Computing category comparison...")
    categories = compute_category_comparison(records)
    write_csv(categories, "category_comparison.csv")

    print("Computing overhead analysis...")
    overhead = compute_overhead(records)
    write_csv(overhead, "overhead_comparison.csv")

    print("Running statistical tests...")
    tests = compute_pairwise_tests(records)
    write_csv(tests, "statistical_tests.csv")

    # Print key findings
    print()
    print("=" * 70)
    print("KEY FINDINGS")
    print("=" * 70)

    # Tool summary
    print("\n--- Overall Tool Comparison ---")
    for ts in tool_summary:
        print(f"  {ts['tool']:15s}: RPS {ts['rps_mean']:6.2f} ± {ts['rps_ci95']:.2f} (95% CI), "
              f"p99 {ts['p99_mean']:.1f}ms, error rate {ts['error_rate_mean']:.4f}")

    cm_rps = mean([r["throughput_rps"] for r in records if r["tool"] == "chaos-mesh"])
    lt_rps = mean([r["throughput_rps"] for r in records if r["tool"] == "litmus"])
    pct_diff = (cm_rps - lt_rps) / lt_rps * 100
    print(f"\n  Chaos Mesh achieves {pct_diff:.1f}% higher throughput than LitmusChaos")

    # Significant differences
    sig_tests = [t for t in tests if t.get("significant", False)]
    print(f"\n--- Statistical Tests ---")
    print(f"  Total tests: {len(tests)}")
    print(f"  Significant after Bonferroni: {len(sig_tests)}")
    for t in sig_tests:
        print(f"    {t['scenario']} {t['metric']}: "
              f"CM={t['cm_mean']}, LT={t['lt_mean']}, "
              f"Δ={t['diff_pct']:.1f}%, d={t['cliffs_delta']:.3f} ({t['effect_size']}), "
              f"p_adj={t['p_adjusted']:.4f}")

    # Category summary
    print("\n--- Category Comparison ---")
    for cat in categories:
        print(f"  {cat['tool']:15s} {cat['category']:15s}: "
              f"RPS {cat['rps_mean']:6.2f}, p99 {cat['p99_mean']:.1f}ms, "
              f"restarts {cat['pod_restarts_mean']:.1f}")

    print()
    print("All results saved to analysis/results/")


if __name__ == "__main__":
    main()
