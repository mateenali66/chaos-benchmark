#!/usr/bin/env python3
"""
Component 2: overhead decomposition (analysis/PREREGISTRATION.md).

Three configurations x 10 repetitions per tool, each tool on its own cluster
(Chaos Mesh on bench-a, LitmusChaos on bench-b):
  (a) baseline: no chaos tool installed, load only.
  (b) idle: tool installed and idle, load only.
  (c) fault: tool plus one representative fault, p1 (pod kill)
      (data-v2/{cluster}/overhead/{tool}-fault/).
Standing overhead is (b) minus (a): idle's `load` phase against baseline's
`load` phase. Fault side effects are (c) minus (b): the fault run's `fault`
phase against idle's `load` phase. Descriptive only, with bootstrap CIs. No
hypothesis test is registered for this component.

CPU and memory are the mean per pod across the social-network namespace
(application pods and the wrk2 load generator), taken over all pods and
timepoints in the phase. The chaos tools' own pods run in other namespaces
and are not included. Values are bootstrap medians with 95% percentile CIs
(10,000 resamples, seed 42).

LitmusChaos is not reported. Its bench-b runs have empty per-run
infrastructure metrics. Its raw sidecars exist, but LitmusChaos runs its
runner and fault pods inside social-network, so a per-pod average from them
would not be comparable with Chaos Mesh's.

Usage:
    python3 analysis/component2_analyze.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

DATA_DIR = Path(__file__).resolve().parent.parent / "data-v2"
OUTPUT_DIR = Path(__file__).resolve().parent / "results"
OUTPUT_DIR.mkdir(exist_ok=True)

CLUSTERS = ("bench-a", "bench-b")
TOOLS = ("chaos-mesh", "litmus")
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 42


def _mean_cpu(phase: dict) -> float | None:
    vals = [float(v) for pod in phase.get("infra_metrics", {}).get("cpu_usage", [])
            for _, v in pod.get("values", [])]
    return (sum(vals) / len(vals)) if vals else None


def _mean_mem_mb(phase: dict) -> float | None:
    vals = [float(v) / (1024 * 1024) for pod in phase.get("infra_metrics", {}).get("memory_usage", [])
            for _, v in pod.get("values", [])]
    return (sum(vals) / len(vals)) if vals else None


def load_config(cluster: str, subdir: str, phase_name: str) -> list[dict]:
    path = DATA_DIR / cluster / "overhead" / subdir
    if not path.is_dir():
        return []
    out = []
    for run_path in sorted(path.glob("run-*.json")):
        with open(run_path) as f:
            d = json.load(f)
        phase = d.get("phases", {}).get(phase_name, {})
        out.append({
            "cluster": cluster, "run": d["metadata"]["run"],
            "cpu": _mean_cpu(phase), "mem_mb": _mean_mem_mb(phase),
        })
    return out


def bootstrap_median_ci(values: list[float]) -> tuple[float, float, float]:
    vals = [v for v in values if v is not None]
    if not vals:
        return float("nan"), float("nan"), float("nan")
    arr = np.asarray(vals)
    med = float(np.median(arr))
    if len(arr) < 2:
        return med, float("nan"), float("nan")
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    resampled = np.median(rng.choice(arr, size=(BOOTSTRAP_RESAMPLES, len(arr)), replace=True), axis=1)
    return med, float(np.percentile(resampled, 2.5)), float(np.percentile(resampled, 97.5))


def bootstrap_diff_ci(a: list[float], b: list[float]) -> tuple[float, float, float]:
    """Median(a) - Median(b) with a bootstrap CI on the difference
    (independent resampling of each group, since baseline/idle/fault are
    different runs, not paired)."""
    a = np.asarray([v for v in a if v is not None])
    b = np.asarray([v for v in b if v is not None])
    if len(a) == 0 or len(b) == 0:
        return float("nan"), float("nan"), float("nan")
    point = float(np.median(a) - np.median(b))
    if len(a) < 2 or len(b) < 2:
        return point, float("nan"), float("nan")
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    ra = np.median(rng.choice(a, size=(BOOTSTRAP_RESAMPLES, len(a)), replace=True), axis=1)
    rb = np.median(rng.choice(b, size=(BOOTSTRAP_RESAMPLES, len(b)), replace=True), axis=1)
    diffs = ra - rb
    return point, float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))


def main():
    rows = []
    skipped_empty = []
    for cluster in CLUSTERS:
        baseline = load_config(cluster, "baseline", "load")
        for tool in TOOLS:
            idle = load_config(cluster, f"{tool}-idle", "load")
            fault = load_config(cluster, f"{tool}-fault", "fault")
            if not idle or not fault:
                continue  # each tool's overhead runs are on one cluster only

            # bench-b's overhead runs (baseline, litmus-idle, litmus-fault)
            # have empty infra_metrics, so LitmusChaos is skipped here (see
            # the module docstring). Their wrk2 throughput and latency are
            # intact but do not answer the CPU/memory question.
            if all(r["cpu"] is None for r in baseline + idle + fault):
                skipped_empty.append((cluster, tool))
                continue

            for metric, key in (("CPU", "cpu"), ("Memory (MB)", "mem_mb")):
                base_vals = [r[key] for r in baseline]
                idle_vals = [r[key] for r in idle]
                fault_vals = [r[key] for r in fault]

                base_med, base_lo, base_hi = bootstrap_median_ci(base_vals)
                idle_med, idle_lo, idle_hi = bootstrap_median_ci(idle_vals)
                fault_med, fault_lo, fault_hi = bootstrap_median_ci(fault_vals)
                standing_diff, standing_lo, standing_hi = bootstrap_diff_ci(idle_vals, base_vals)
                fault_diff, fault_dlo, fault_dhi = bootstrap_diff_ci(fault_vals, idle_vals)

                rows.append({
                    "cluster": cluster, "tool": tool, "metric": metric,
                    "n_baseline": len(base_vals), "n_idle": len(idle_vals), "n_fault": len(fault_vals),
                    "baseline_median": round(base_med, 4), "baseline_ci_lo": round(base_lo, 4) if base_lo == base_lo else None,
                    "baseline_ci_hi": round(base_hi, 4) if base_hi == base_hi else None,
                    "idle_median": round(idle_med, 4), "idle_ci_lo": round(idle_lo, 4) if idle_lo == idle_lo else None,
                    "idle_ci_hi": round(idle_hi, 4) if idle_hi == idle_hi else None,
                    "fault_median": round(fault_med, 4), "fault_ci_lo": round(fault_lo, 4) if fault_lo == fault_lo else None,
                    "fault_ci_hi": round(fault_hi, 4) if fault_hi == fault_hi else None,
                    "standing_overhead_idle_minus_baseline": round(standing_diff, 4),
                    "standing_overhead_ci_lo": round(standing_lo, 4) if standing_lo == standing_lo else None,
                    "standing_overhead_ci_hi": round(standing_hi, 4) if standing_hi == standing_hi else None,
                    "fault_side_effect_fault_minus_idle": round(fault_diff, 4),
                    "fault_side_effect_ci_lo": round(fault_dlo, 4) if fault_dlo == fault_dlo else None,
                    "fault_side_effect_ci_hi": round(fault_dhi, 4) if fault_dhi == fault_dhi else None,
                })

    if skipped_empty:
        print(f"SKIPPED (empty Prometheus infra_metrics, disclosed limitation): {skipped_empty}")
    print(f"{len(rows)} (cluster, tool, metric) rows computed")
    for r in rows:
        print(f"  {r['cluster']}/{r['tool']}/{r['metric']}: "
              f"standing_overhead={r['standing_overhead_idle_minus_baseline']} "
              f"[{r['standing_overhead_ci_lo']}, {r['standing_overhead_ci_hi']}], "
              f"fault_side_effect={r['fault_side_effect_fault_minus_idle']} "
              f"[{r['fault_side_effect_ci_lo']}, {r['fault_side_effect_ci_hi']}]")

    out_path = OUTPUT_DIR / "component2_overhead.csv"
    import csv
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nWritten to {out_path}")


if __name__ == "__main__":
    main()
