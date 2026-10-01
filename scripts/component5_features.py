#!/usr/bin/env python3
"""
Component 5 feature extraction (analysis/PREREGISTRATION.md, Component 5).

Loads a Component 1 run's Prometheus timeseries sidecar
(data-v2/{cluster}/{tool}/{scenario}/run-N.timeseries.json.gz) and its
run-N.json, and builds an aligned (T x F) feature matrix on a 5 s grid plus
a per-timestep fault label for per-run detector training.

Features are infrastructure series only: container CPU, container memory,
container network rx and tx, pod restarts, node CPU and node memory. The
sidecars also hold Envoy-based HTTP queries (http_request_rate,
http_error_rate, http_latency_bucket), but these return no series in any
run, so they are not used. Most runs have all 7 features. 25 early runs have
5, because their network series are empty (collected before the
network-query fix). A feature with no data on a run is dropped for that run.

Detectors train on the run's own baseline phase only, taken from
run-N.json. The sidecar's wider pre-baseline buffer (window_start) is not
used, because its first ~40 s overlap the excluded warm-up traffic
(PREREGISTRATION.md, Component 1 amendment item 5).
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data-v2"

# Infrastructure metric families used as features (see module docstring).
FEATURE_METRICS = [
    "container_cpu_usage",
    "container_memory_working_set",
    "container_network_rx_bytes",
    "container_network_tx_bytes",
    "pod_restarts_total",
    "node_cpu_utilization",
    "node_memory_used_bytes",
]

RESAMPLE_STEP_S = 5  # matches the sidecar's own Prometheus scrape step


def find_run_pairs(clusters=("bench-a", "bench-b"), tools=("chaos-mesh", "litmus")):
    """Yield (run_json_path, timeseries_path) for every Component 1 run that
    has both files present."""
    for cluster in clusters:
        for tool in tools:
            tool_dir = DATA_DIR / cluster / tool
            if not tool_dir.is_dir():
                continue
            for run_json in sorted(tool_dir.glob("*/run-*.json")):
                ts_path = run_json.with_suffix("").with_suffix(".timeseries.json.gz")
                if ts_path.exists():
                    yield run_json, ts_path


def _series_to_df(result: list[dict], agg: str) -> pd.Series | None:
    """Aggregate every pod/instance's [(ts, val), ...] series in a single
    Prometheus `result` list onto one common 5s grid, then combine across
    pods with `agg` ("sum" or "mean"). Returns None if the result is empty."""
    if not result:
        return None
    frames = []
    for s in result:
        vals = s.get("values", [])
        if not vals:
            continue
        idx = pd.to_datetime([v[0] for v in vals], unit="s")
        try:
            data = [float(v[1]) for v in vals]
        except (TypeError, ValueError):
            continue
        ser = pd.Series(data, index=idx)
        ser = ser[~ser.index.duplicated(keep="last")].sort_index()
        frames.append(ser)
    if not frames:
        return None
    df = pd.concat(frames, axis=1)
    df = df.resample(f"{RESAMPLE_STEP_S}s").mean().interpolate(limit_direction="both")
    combined = df.sum(axis=1) if agg == "sum" else df.mean(axis=1)
    return combined


def load_run_features(run_json_path: Path, ts_path: Path) -> dict | None:
    """Build the feature matrix and labels for one run.

    Returns None if the run is unusable: missing phases, fewer than 4
    populated features, or too few fault, non-fault or baseline timesteps.
    Otherwise returns:
      {
        "grid": DatetimeIndex,                  # common 5 s timestamps
        "X": np.ndarray (T, F),                 # features in FEATURE_METRICS order, empty ones dropped
        "feature_names": list[str],             # column names of X
        "label": np.ndarray (T,) bool,          # True during [fault.start, fault.end)
        "baseline_mask": np.ndarray (T,) bool,  # True during [baseline.start, baseline.end)
        "metadata": {...},
      }
    """
    with open(run_json_path) as f:
        run = json.load(f)
    phases = run.get("phases", {})
    baseline = phases.get("baseline", {})
    fault = phases.get("fault", {})
    recovery = phases.get("recovery", {})
    if not (baseline.get("start") and fault.get("start") and fault.get("end")):
        return None

    with gzip.open(ts_path) as f:
        ts = json.load(f)
    metrics = ts.get("metrics", {})

    series_by_metric = {}
    for name in FEATURE_METRICS:
        agg = "mean" if name in ("node_cpu_utilization", "node_memory_used_bytes") else "sum"
        result = metrics.get(name, {}).get("result", [])
        series_by_metric[name] = _series_to_df(result, agg)

    available = {k: v for k, v in series_by_metric.items() if v is not None}
    if len(available) < 4:  # need a reasonably multivariate signal to be worth modeling
        return None

    grid_start = pd.to_datetime(baseline["start"], unit="s").floor(f"{RESAMPLE_STEP_S}s")
    grid_end = pd.to_datetime(recovery.get("end", fault["end"]), unit="s").ceil(f"{RESAMPLE_STEP_S}s")
    grid = pd.date_range(grid_start, grid_end, freq=f"{RESAMPLE_STEP_S}s")

    cols = []
    for name in FEATURE_METRICS:
        ser = series_by_metric.get(name)
        if ser is None:
            cols.append(pd.Series(np.nan, index=grid))
        else:
            cols.append(ser.reindex(grid, method="nearest", tolerance=pd.Timedelta(seconds=RESAMPLE_STEP_S * 2)))
    X = pd.concat(cols, axis=1)
    X.columns = FEATURE_METRICS
    # Drop feature columns that are entirely missing on this grid, then
    # fill any remaining sparse gaps by interpolation (never by dropping
    # timesteps, since labels/timestamps must stay aligned across runs).
    X = X.dropna(axis=1, how="all")
    if X.shape[1] < 4:
        return None
    X = X.interpolate(limit_direction="both")
    if X.isna().any().any():
        return None

    fault_start = pd.to_datetime(fault["start"], unit="s")
    fault_end = pd.to_datetime(fault["end"], unit="s")
    baseline_start = pd.to_datetime(baseline["start"], unit="s")
    baseline_end = pd.to_datetime(baseline.get("end", fault["start"]), unit="s")

    label = (grid >= fault_start) & (grid < fault_end)
    baseline_mask = (grid >= baseline_start) & (grid < baseline_end)

    if label.sum() < 2 or (~label).sum() < 2 or baseline_mask.sum() < 10:
        return None  # not enough of either class, or too little training data

    return {
        "grid": grid,
        "X": X.to_numpy(dtype=float),
        "feature_names": list(X.columns),
        "label": np.asarray(label),
        "baseline_mask": np.asarray(baseline_mask),
        "metadata": {
            "tool": run["metadata"]["tool"],
            "scenario": run["metadata"]["scenario"],
            "run": run["metadata"]["run"],
            "cluster": run["metadata"].get("cluster"),
        },
    }
