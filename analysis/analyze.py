#!/usr/bin/env python3
"""
Component 1 statistical analysis (analysis/PREREGISTRATION.md).

Aggregates the real 720 experiment JSON files (2 tools x 12 scenarios x
n=30 reps each, crossover-allocated across bench-a/bench-b per Component 1
amendment item 1 -- each cluster runs BOTH tools, 15 reps each, in
counterbalanced order) from
data-v2/{bench-a,bench-b}/{chaos-mesh,litmus}/{scenario}/run-*.json,
computes the pre-registered confirmatory analyses, and writes the CSVs that
tables.py and figures.py read. This script is the single source of
statistical truth for Component 1 -- tables/figures must not independently
reload raw JSON or recompute statistics, to avoid the exact
figure/table/text inconsistency risk a prior paper in this portfolio was
rejected over.

Confirmatory analyses (PREREGISTRATION.md, "Confirmatory analyses"):
  1. Two-sided Mann-Whitney U comparing tools, per scenario, n=30 per arm
     (UNPAIRED -- this replaces a prior draft of this script that used a
     paired Wilcoxon signed-rank test on n=5, the exact design a peer
     reviewer flagged as underpowered in this paper's original submission;
     Mann-Whitney U is what is actually pre-registered now).
  2. Holm-Bonferroni within each metric family (12 scenario-level tests per
     metric; the primary-metric family is throughput_rps, confirmatory;
     secondary-metric families -- p99 latency, error rate, pod restarts --
     are each their own family, reported as supporting evidence).
  3. Cliff's delta per scenario, 95% BCa bootstrap CI (10,000 resamples).
  4. Medians/IQRs (not means) reported for all skewed metrics; percentage
     differences reported only alongside their CI.

NOTE on the primary metric: both tools' wrk2 load generator runs as a
SINGLE job spanning baseline+fault+recovery (~480s: 300+120+60). Amendment
item 6c discloses this same single-wrk2-job characteristic for Component 4's
ground truth; it applies identically here -- throughput_rps is an aggregate
over the whole protocol window, not isolated to the ~120s fault phase alone.
This does not invalidate the tool-vs-tool comparison (both arms are measured
identically, so the comparison is apples-to-apples), but the manuscript
methods section must describe the metric accurately rather than as literally
fault-phase-isolated.

NOTE on recovery time: no continuous "time to recover" field is recorded
anywhere in the pipeline (only Component 3's boolean recovery_over_60s
signal exists, and that's a different component). This script computes one
from the recorded per-phase CPU timeseries (infra_metrics.cpu_usage): the
elapsed time from fault end until the mean CPU across faulted pods first
returns to within 20% of that same run's own baseline-phase CPU mean
(matching Component 3's recovery_over_60s threshold, chaoslib.py, for
consistency), right-censored at the 60s recovery-phase length if it never
recovers within the window. This is a NEW operational definition invented
for this analysis, not previously specified anywhere in
PREREGISTRATION.md, and must be disclosed as such in the manuscript before
being reported as a confirmatory or even labelled secondary result.

Usage:
    python3 analysis/analyze.py

Outputs (analysis/results/):
    summary_stats.csv        per (tool, scenario) descriptive stats
    tool_summary.csv         per-tool overall descriptive stats
    category_comparison.csv  per (tool, category) aggregation
    cluster_robustness.csv   per (tool, scenario, cluster) crossover check
    statistical_tests.csv    Mann-Whitney U + Holm-Bonferroni + Cliff's delta
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.stats import mannwhitneyu, bootstrap

DATA_DIR = Path(__file__).resolve().parent.parent / "data-v2"
OUTPUT_DIR = Path(__file__).resolve().parent / "results"
OUTPUT_DIR.mkdir(exist_ok=True)

CLUSTERS = ("bench-a", "bench-b")
TOOLS = ("chaos-mesh", "litmus")
N_PER_ARM = 30
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 42
RECOVERY_CPU_TOLERANCE = 1.2  # matches chaoslib.py's recovery_over_60s threshold
RECOVERY_WINDOW_S = 60

SCENARIO_NAMES = {
    "p1": "Pod Kill", "p2": "Container Kill", "p3": "Pod Failure",
    "n1": "Latency 50ms", "n2": "Latency 100ms", "n3": "Latency 300ms",
    "n4": "Packet Loss 5%", "n5": "Network Partition",
    "r1": "CPU Stress 80%", "r2": "Memory Pressure 80%",
    "a1": "HTTP Abort 503", "a2": "gRPC Unavailable",
}
SCENARIO_CATEGORIES = {
    "p1": "Pod/Container", "p2": "Pod/Container", "p3": "Pod/Container",
    "n1": "Network", "n2": "Network", "n3": "Network", "n4": "Network", "n5": "Network",
    "r1": "Resource", "r2": "Resource",
    "a1": "Application", "a2": "Application",
}
TOOL_LABELS = {"chaos-mesh": "Chaos Mesh", "litmus": "LitmusChaos"}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _cpu_series(phase_data: dict) -> list[tuple[float, float]]:
    """Flatten a phase's per-pod cpu_usage series into [(ts, value), ...]
    across all pods, sorted by timestamp."""
    infra = phase_data.get("infra_metrics") or {}
    out = []
    for pod in infra.get("cpu_usage", []):
        for ts, val in pod.get("values", []):
            try:
                out.append((float(ts), float(val)))
            except (TypeError, ValueError):
                pass
    return sorted(out)


def _mean_cpu(phase_data: dict) -> float | None:
    vals = [v for _, v in _cpu_series(phase_data)]
    return (sum(vals) / len(vals)) if vals else None


def _mean_mem_mb(phase_data: dict) -> float | None:
    infra = phase_data.get("infra_metrics") or {}
    vals = []
    for pod in infra.get("memory_usage", []):
        for _, val in pod.get("values", []):
            try:
                vals.append(float(val) / (1024 * 1024))
            except (TypeError, ValueError):
                pass
    return (sum(vals) / len(vals)) if vals else None


def _recovery_time_s(record: dict, baseline_cpu: float | None) -> float | None:
    """Elapsed seconds from fault end until mean CPU across recovery-phase
    samples first drops back to <= baseline_cpu * RECOVERY_CPU_TOLERANCE.
    Right-censored at RECOVERY_WINDOW_S (reported as exactly that value,
    i.e. "did not recover within the window") if the threshold is never
    crossed, or if baseline_cpu is unavailable/zero."""
    recovery = record["_phases"].get("recovery", {})
    fault_end = recovery.get("start")
    series = _cpu_series(recovery)
    if not baseline_cpu or fault_end is None or not series:
        return None
    threshold = baseline_cpu * RECOVERY_CPU_TOLERANCE
    for ts, val in series:
        if val <= threshold:
            return max(0.0, ts - fault_end)
    return float(RECOVERY_WINDOW_S)


def load_all_experiments() -> list[dict]:
    records = []
    for cluster in CLUSTERS:
        for tool in TOOLS:
            tool_dir = DATA_DIR / cluster / tool
            if not tool_dir.is_dir():
                continue
            for run_path in sorted(tool_dir.glob("*/run-*.json")):
                with open(run_path) as f:
                    d = json.load(f)
                m, w = d["metadata"], d["wrk2"]
                dr = d.get("derived", {})
                phases = d.get("phases", {})
                lat = w.get("latency_ms", {})
                errs = w.get("errors", {})
                requests_total = w.get("requests_total", 0) or 0
                error_count = sum(errs.get(k, 0) for k in
                                   ("connect", "read", "write", "timeout", "http_non2xx3xx"))

                baseline_cpu = _mean_cpu(phases.get("baseline", {}))
                fault_cpu = _mean_cpu(phases.get("fault", {}))
                baseline_mem = _mean_mem_mb(phases.get("baseline", {}))
                fault_mem = _mean_mem_mb(phases.get("fault", {}))

                rec = {
                    "cluster": cluster,
                    "tool": tool,
                    "scenario": m["scenario"],
                    "scenario_name": SCENARIO_NAMES.get(m["scenario"], m["scenario"]),
                    "category": SCENARIO_CATEGORIES.get(m["scenario"], "Unknown"),
                    "run": m["run"],
                    "file": str(run_path),
                    "throughput_rps": w["throughput_rps"],
                    "latency_p50": lat.get("p50"),
                    "latency_p99": lat.get("p99"),
                    "latency_mean": lat.get("mean"),
                    "requests_total": requests_total,
                    "error_rate": (error_count / requests_total) if requests_total else 0.0,
                    "pod_restarts": dr.get("pod_restarts_during_fault", 0),
                    "cpu_spike_pct": dr.get("cpu_spike_pct"),
                    "memory_spike_mb": dr.get("memory_spike_mb"),
                    "baseline_cpu_mean": baseline_cpu,
                    "fault_cpu_mean": fault_cpu,
                    "baseline_mem_mb": baseline_mem,
                    "fault_mem_mb": fault_mem,
                    "_phases": phases,
                }
                rec["recovery_time_s"] = _recovery_time_s(rec, baseline_cpu)
                records.append(rec)
    return records


# ---------------------------------------------------------------------------
# Statistical helpers
# ---------------------------------------------------------------------------

def median_iqr(xs: list[float]) -> tuple[float, float, float]:
    xs = [x for x in xs if x is not None]
    if not xs:
        return float("nan"), float("nan"), float("nan")
    arr = np.asarray(xs, dtype=float)
    return float(np.median(arr)), float(np.percentile(arr, 25)), float(np.percentile(arr, 75))


def cliffs_delta(x: list[float], y: list[float]) -> tuple[float, str]:
    x = np.asarray([v for v in x if v is not None], dtype=float)
    y = np.asarray([v for v in y if v is not None], dtype=float)
    if len(x) == 0 or len(y) == 0:
        return float("nan"), "negligible"
    more = (x[:, None] > y[None, :]).sum()
    less = (x[:, None] < y[None, :]).sum()
    delta = (more - less) / (len(x) * len(y))
    a = abs(delta)
    mag = "negligible" if a < 0.147 else "small" if a < 0.33 else "medium" if a < 0.474 else "large"
    return float(delta), mag


def cliffs_delta_stat(x: np.ndarray, y: np.ndarray) -> float:
    """Vectorized Cliff's delta for scipy.stats.bootstrap (two-sample,
    paired=False; x and y are resampled independently each draw)."""
    more = (x[:, None] > y[None, :]).sum()
    less = (x[:, None] < y[None, :]).sum()
    return (more - less) / (len(x) * len(y))


def _percentile_bootstrap_ci(x: np.ndarray, y: np.ndarray, rng) -> tuple[float, float]:
    scores = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        xs = rng.choice(x, size=len(x), replace=True)
        ys = rng.choice(y, size=len(y), replace=True)
        scores.append(cliffs_delta_stat(xs, ys))
    scores.sort()
    lo = scores[int(0.025 * len(scores))]
    hi = scores[int(0.975 * len(scores)) - 1]
    return float(lo), float(hi)


def cliffs_delta_bca_ci(x: list[float], y: list[float]) -> tuple[float, float, str]:
    """Returns (lo, hi, method). BCa's acceleration constant is undefined
    for degenerate/perfectly-separated samples (delta = +-1.0, the jackknife
    denominator divides by zero) -- scipy warns rather than raises in that
    case and returns NaN, so NaN must be checked explicitly, not just
    caught via exception, or the CI silently vanishes for exactly the
    highest-stakes (most separated, most significant) results."""
    x = np.asarray([v for v in x if v is not None], dtype=float)
    y = np.asarray([v for v in y if v is not None], dtype=float)
    if len(x) < 2 or len(y) < 2:
        return float("nan"), float("nan"), "insufficient_n"
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    try:
        res = bootstrap((x, y), cliffs_delta_stat, n_resamples=BOOTSTRAP_RESAMPLES,
                         paired=False, vectorized=False, method="BCa",
                         random_state=rng, confidence_level=0.95)
        lo, hi = float(res.confidence_interval.low), float(res.confidence_interval.high)
        if lo == lo and hi == hi:  # not NaN
            return lo, hi, "BCa"
    except Exception:
        pass
    # BCa degenerate (perfect/near-perfect separation) or raised: fall back
    # to a plain percentile bootstrap rather than silently dropping the CI.
    lo, hi = _percentile_bootstrap_ci(x, y, rng)
    return lo, hi, "percentile_fallback"


def holm_bonferroni(p_values: list[float], alpha: float = 0.05) -> tuple[list[float], list[bool]]:
    """Holm-Bonferroni step-down correction. Returns (adjusted_p, significant)
    in the ORIGINAL input order."""
    m = len(p_values)
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [0.0] * m
    running_max = 0.0
    for rank, idx in enumerate(order):
        adj = min((m - rank) * p_values[idx], 1.0)
        running_max = max(running_max, adj)  # enforce monotonicity
        adjusted[idx] = running_max
    significant = [a < alpha for a in adjusted]
    return adjusted, significant


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def compute_summary_stats(records: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for r in records:
        groups[(r["tool"], r["scenario"])].append(r)

    rows = []
    for (tool, scenario), recs in sorted(groups.items()):
        rps_med, rps_q1, rps_q3 = median_iqr([r["throughput_rps"] for r in recs])
        p99_med, p99_q1, p99_q3 = median_iqr([r["latency_p99"] for r in recs])
        err_med, err_q1, err_q3 = median_iqr([r["error_rate"] for r in recs])
        rec_med, rec_q1, rec_q3 = median_iqr([r["recovery_time_s"] for r in recs])
        rows.append({
            "tool": TOOL_LABELS[tool], "scenario": scenario,
            "scenario_name": SCENARIO_NAMES.get(scenario, scenario),
            "category": SCENARIO_CATEGORIES.get(scenario, ""),
            "n": len(recs),
            "rps_median": round(rps_med, 2), "rps_q1": round(rps_q1, 2), "rps_q3": round(rps_q3, 2),
            "p99_median": round(p99_med, 2), "p99_q1": round(p99_q1, 2), "p99_q3": round(p99_q3, 2),
            "error_rate_median": round(err_med, 4),
            "recovery_time_median_s": round(rec_med, 1) if rec_med == rec_med else None,
            "pod_restarts_median": median_iqr([r["pod_restarts"] for r in recs])[0],
        })
    return rows


def compute_cluster_robustness(records: list[dict]) -> list[dict]:
    """Per-cluster stratified view (Component 1 amendment item 1's
    robustness check for the crossover allocation)."""
    groups = defaultdict(list)
    for r in records:
        groups[(r["tool"], r["scenario"], r["cluster"])].append(r)
    rows = []
    for (tool, scenario, cluster), recs in sorted(groups.items()):
        med, q1, q3 = median_iqr([r["throughput_rps"] for r in recs])
        rows.append({
            "tool": TOOL_LABELS[tool], "scenario": scenario, "cluster": cluster,
            "n": len(recs), "rps_median": round(med, 2),
            "rps_q1": round(q1, 2), "rps_q3": round(q3, 2),
        })
    return rows


def compute_tool_summary(records: list[dict]) -> list[dict]:
    rows = []
    for tool in TOOLS:
        recs = [r for r in records if r["tool"] == tool]
        rps_med, rps_q1, rps_q3 = median_iqr([r["throughput_rps"] for r in recs])
        p99_med, p99_q1, p99_q3 = median_iqr([r["latency_p99"] for r in recs])
        err_med, _, _ = median_iqr([r["error_rate"] for r in recs])
        rows.append({
            "tool": TOOL_LABELS[tool], "n": len(recs),
            "rps_median": round(rps_med, 2), "rps_q1": round(rps_q1, 2), "rps_q3": round(rps_q3, 2),
            "p99_median": round(p99_med, 2), "p99_q1": round(p99_q1, 2), "p99_q3": round(p99_q3, 2),
            "error_rate_median": round(err_med, 4),
            "pod_restarts_total": sum(r["pod_restarts"] for r in recs),
        })
    return rows


def compute_category_comparison(records: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for r in records:
        groups[(r["tool"], r["category"])].append(r)
    rows = []
    for (tool, cat), recs in sorted(groups.items()):
        rps_med, _, _ = median_iqr([r["throughput_rps"] for r in recs])
        p99_med, _, _ = median_iqr([r["latency_p99"] for r in recs])
        err_med, _, _ = median_iqr([r["error_rate"] for r in recs])
        rows.append({
            "tool": TOOL_LABELS[tool], "category": cat, "n_experiments": len(recs),
            "rps_median": round(rps_med, 2), "p99_median": round(p99_med, 2),
            "error_rate_median": round(err_med, 4),
            "pod_restarts_median": median_iqr([r["pod_restarts"] for r in recs])[0],
        })
    return rows


METRIC_FAMILIES = {
    "throughput_rps": ("Throughput (rps)", True),   # (label, is_primary)
    "latency_p99": ("Latency p99 (ms)", False),
    "error_rate": ("Error rate", False),
    "recovery_time_s": ("Recovery time (s)", False),
    "pod_restarts": ("Pod restarts", False),
}


def compute_statistical_tests(records: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for r in records:
        groups[(r["tool"], r["scenario"])].append(r)

    scenarios = sorted(SCENARIO_NAMES)
    per_family_rows: dict[str, list[dict]] = defaultdict(list)

    for metric, (label, _is_primary) in METRIC_FAMILIES.items():
        for scenario in scenarios:
            cm = groups.get(("chaos-mesh", scenario), [])
            lt = groups.get(("litmus", scenario), [])
            cm_vals = [r[metric] for r in cm if r[metric] is not None]
            lt_vals = [r[metric] for r in lt if r[metric] is not None]
            if len(cm_vals) < 2 or len(lt_vals) < 2:
                continue

            stat, p = mannwhitneyu(cm_vals, lt_vals, alternative="two-sided")
            delta, mag = cliffs_delta(cm_vals, lt_vals)
            ci_lo, ci_hi, ci_method = cliffs_delta_bca_ci(cm_vals, lt_vals)
            cm_med, cm_q1, cm_q3 = median_iqr(cm_vals)
            lt_med, lt_q1, lt_q3 = median_iqr(lt_vals)
            pct_diff = ((cm_med - lt_med) / lt_med * 100) if lt_med else float("nan")

            per_family_rows[metric].append({
                "scenario": scenario, "scenario_name": SCENARIO_NAMES[scenario],
                "metric": label, "n_cm": len(cm_vals), "n_lt": len(lt_vals),
                "cm_median": round(cm_med, 3), "cm_q1": round(cm_q1, 3), "cm_q3": round(cm_q3, 3),
                "lt_median": round(lt_med, 3), "lt_q1": round(lt_q1, 3), "lt_q3": round(lt_q3, 3),
                "diff_pct": round(pct_diff, 1) if pct_diff == pct_diff else None,
                "U_statistic": round(float(stat), 2), "p_value": round(float(p), 5),
                "cliffs_delta": round(delta, 3), "effect_size": mag,
                "delta_ci_95_lo": round(ci_lo, 3) if ci_lo == ci_lo else None,
                "delta_ci_95_hi": round(ci_hi, 3) if ci_hi == ci_hi else None,
                "delta_ci_method": ci_method,
            })

    # Holm-Bonferroni WITHIN each metric family separately (PREREGISTRATION.md,
    # Confirmatory analysis #2: "12 scenario-level tests per metric").
    all_rows = []
    for metric, rows in per_family_rows.items():
        p_values = [row["p_value"] for row in rows]
        adjusted, significant = holm_bonferroni(p_values)
        for row, adj, sig in zip(rows, adjusted, significant):
            row["p_adjusted_holm"] = round(adj, 5)
            row["significant_after_holm"] = sig
            row["is_primary_family"] = METRIC_FAMILIES[metric][1]
            all_rows.append(row)

    return all_rows


def write_csv(rows: list[dict], filename: str) -> None:
    if not rows:
        print(f"  (skipped, no rows): {filename}")
        return
    filepath = OUTPUT_DIR / filename
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Written: {filepath} ({len(rows)} rows)")


def write_runs_long(records: list[dict]) -> None:
    """One row per run, all metrics -- the raw-distribution source for
    figures.py (boxplots etc. need per-run values, not just medians/IQRs),
    so figures never re-parse the original run JSON themselves."""
    fields = ["cluster", "tool", "scenario", "scenario_name", "category", "run",
              "throughput_rps", "latency_p50", "latency_p99", "latency_mean",
              "error_rate", "pod_restarts", "cpu_spike_pct", "memory_spike_mb",
              "baseline_cpu_mean", "fault_cpu_mean", "baseline_mem_mb", "fault_mem_mb",
              "recovery_time_s"]
    rows = [{k: r[k] for k in fields} for r in records]
    write_csv(rows, "runs_long.csv")


def main():
    print("Loading Component 1 experiment data (data-v2/{bench-a,bench-b})...")
    records = load_all_experiments()
    print(f"  Loaded {len(records)} runs")
    by_arm = defaultdict(int)
    for r in records:
        by_arm[(r["tool"], r["scenario"])] += 1
    off_target = {k: v for k, v in by_arm.items() if v != N_PER_ARM}
    if off_target:
        print(f"  WARNING: {len(off_target)} (tool, scenario) cells do not have exactly "
              f"n={N_PER_ARM}: {off_target}")
    print()

    print("Writing per-run long-format CSV (raw-distribution source for figures.py)...")
    write_runs_long(records)

    print("Computing summary statistics...")
    write_csv(compute_summary_stats(records), "summary_stats.csv")

    print("Computing tool-level summary...")
    write_csv(compute_tool_summary(records), "tool_summary.csv")

    print("Computing category comparison...")
    write_csv(compute_category_comparison(records), "category_comparison.csv")

    print("Computing per-cluster crossover robustness check...")
    write_csv(compute_cluster_robustness(records), "cluster_robustness.csv")

    print("Running confirmatory statistical tests "
          "(Mann-Whitney U, Holm-Bonferroni per family, Cliff's delta + BCa CI)...")
    tests = compute_statistical_tests(records)
    write_csv(tests, "statistical_tests.csv")

    print()
    print("=" * 70)
    print("KEY FINDINGS (primary family: throughput_rps only)")
    print("=" * 70)
    primary = [t for t in tests if t["metric"] == "Throughput (rps)"]
    sig = [t for t in primary if t["significant_after_holm"]]
    print(f"  {len(primary)} scenarios tested, {len(sig)} significant after Holm-Bonferroni")
    for t in sig:
        print(f"    {t['scenario']} ({t['scenario_name']}): CM={t['cm_median']} vs "
              f"LT={t['lt_median']} rps, delta={t['cliffs_delta']} ({t['effect_size']}) "
              f"CI=[{t['delta_ci_95_lo']}, {t['delta_ci_95_hi']}], p_holm={t['p_adjusted_holm']}")
    if not sig:
        print("    (none -- see analysis/results/statistical_tests.csv for full per-scenario results)")
    print()
    print("All results saved to analysis/results/")


if __name__ == "__main__":
    main()
