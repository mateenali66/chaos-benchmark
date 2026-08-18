#!/usr/bin/env python3
"""
Component 4 mechanical scoring (analysis/PREREGISTRATION.md, amendment items
6-7).

Scores each of the 540 real Component 4 hypothesis-generation samples
(data-v2/ml/hypotheses/{arm}/candidate-{id}/sample-{n}.json) against real
ground truth. Per amendment item 7 (2026-08-18, superseding item 6's original
pooling rule after a demonstrated confound was found -- see PREREGISTRATION.md
for the full account, including candidate C14's classification flipping
between sources), ground truth for each of the 36 fault-space candidates
comes from Component 3's 500 campaign injections ONLY
(data-v2/ml/campaigns/*/campaign-*/injection-N.json), matched by
candidate_id. Component 1's 720 runs are no longer pooled in: their protocol
window (540s, fault = 22% of window) differs from Component 3's (300s, fault
= 40%), and pooling let Component 1's much larger n drown out Component 3's
real, protocol-matched signal for the 3 candidates where both sources exist.
`component1_runs_for()` is retained only for a potential future
robustness-check appendix, not used for the primary ground truth. A
candidate with zero real executions is EXCLUDED from scoring, never imputed
(item 6); Component 3 alone covers 36/36, so no candidate is currently
excluded.

Primary metric (PREREGISTRATION.md): balanced accuracy of throughput_direction
(prediction i) per model, with 95% percentile bootstrap CIs (10,000
resamples, fixed seed 42 -- item 7c; this is registered explicitly for
Component 4 and is NOT the same method as Component 1's BCa CIs). Baselines:
practitioner_heuristic (scripts/practitioner_heuristic.py, frozen rules),
chance (0.5 by construction for balanced accuracy on a binary task), and a
trivial always-predict-the-majority-ground-truth-class baseline (item 7e).
Each arm's raw predicted-label distribution is also reported so a
constant/degenerate predictor -- which mechanically produces exactly 0.5
balanced accuracy with zero bootstrap CI width, indistinguishable from
genuine chance performance in the summary numbers alone -- is visible
without inspecting raw JSON.

Secondary (exploratory, no CI/significance claims made): precision/recall of
degraded_services (ii) against the ground-truth CPU-spike set, and
calibration of weakness_signal_present (iii) against any_violation.

Ground-truth throughput_direction rule (item 6c): degrade if
(119.7 - median_run_throughput_rps) / 119.7 > 0.10, else no_meaningful_change.
119.7 rps is Component 2's real steady-state baseline
(experiments/component4-baseline-summary.json).

Usage:
  python3 scripts/score-hypotheses.py [--out analysis/results/component4-scoring.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXPERIMENTS_DIR = PROJECT_ROOT / "experiments"
DATA_DIR = PROJECT_ROOT / "data-v2"
HYPOTHESES_DIR = DATA_DIR / "ml" / "hypotheses"
CAMPAIGNS_DIR = DATA_DIR / "ml" / "campaigns"
FAULT_SPACE_FILE = EXPERIMENTS_DIR / "fault-space.yaml"
SCENARIOS_FILE = EXPERIMENTS_DIR / "scenarios.yaml"
BASELINE_SUMMARY_FILE = EXPERIMENTS_DIR / "component4-baseline-summary.json"

BASELINE_THROUGHPUT_RPS = 119.7
DEGRADE_THRESHOLD = 0.10  # Amendment 6c
BOOTSTRAP_RESAMPLES = 10_000

sys.path.insert(0, str(Path(__file__).resolve().parent))
import practitioner_heuristic  # noqa: E402

# Component 1's 12 original (scenario_id -> scenario_template) mapping,
# read directly off experiments/scenarios.yaml's own action/parameters
# fields (not fault-space.yaml, which only lists the 36 campaign candidates).
SCENARIO_TEMPLATE_MAP = {
    "p1": "pod-kill", "p2": "container-kill", "p3": "pod-failure",
    "n1": "latency-50ms", "n2": "latency-100ms", "n3": "latency-300ms",
    "n4": "packet-loss-5pct", "n5": "network-partition",
    "r1": "cpu-stress-80pct", "r2": "memory-pressure-80pct",
    "a1": "http-abort-503", "a2": "grpc-unavailable",
}


def load_fault_space() -> list[dict]:
    with open(FAULT_SPACE_FILE) as f:
        doc = yaml.safe_load(f)
    return doc["candidates"]


def load_scenarios() -> dict[str, dict]:
    """Keyed by lowercased id ("p1") to match both SCENARIO_TEMPLATE_MAP and
    the data-v2/{cluster}/{tool}/{id}/ directory naming convention -- the
    scenarios.yaml `id` field itself is uppercase ("P1")."""
    with open(SCENARIOS_FILE) as f:
        doc = yaml.safe_load(f)
    return {s["id"].lower(): s for s in doc["scenarios"]}


def component1_runs_for(scenario_template: str, target_service: str, scenarios: dict) -> list[Path]:
    """Every Component 1 run-N.json (either tool, either cluster) whose
    scenario matches this exact (template, service) pair."""
    matches = []
    for sid, template in SCENARIO_TEMPLATE_MAP.items():
        if template != scenario_template:
            continue
        scenario = scenarios.get(sid)
        if not scenario or scenario["target"]["name"] != target_service:
            continue
        for cluster in ("bench-a", "bench-b"):
            for tool in ("chaos-mesh", "litmus"):
                scenario_dir = DATA_DIR / cluster / tool / sid
                if scenario_dir.is_dir():
                    matches.extend(sorted(scenario_dir.glob("run-*.json")))
    return matches


def component3_injections_for(candidate_id: str) -> list[Path]:
    return [
        p for p in CAMPAIGNS_DIR.glob("*/campaign-*/injection-*.json")
        if _injection_candidate_id(p) == candidate_id
    ]


_injection_id_cache: dict[Path, str] = {}


def _injection_candidate_id(path: Path) -> str | None:
    if path not in _injection_id_cache:
        try:
            with open(path) as f:
                d = json.load(f)
            _injection_id_cache[path] = d.get("metadata", {}).get("candidate_id")
        except (json.JSONDecodeError, OSError):
            _injection_id_cache[path] = None
    return _injection_id_cache[path]


def real_throughput_and_violation(path: Path) -> tuple[float | None, bool | None]:
    """Extract (throughput_rps, any_violation) from a real run/injection
    file. Component 1 run files have no weakness_signals block (that's a
    Component 3 concept); any_violation is None for those, and
    weakness_signal_present ground truth is scored only from Component 3
    data plus Component 1's own pod_restarts/error signal as a fallback."""
    try:
        with open(path) as f:
            d = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None, None
    wrk2 = d.get("wrk2", {})
    throughput = wrk2.get("throughput_rps")
    weakness_signals = d.get("weakness_signals")
    if weakness_signals:
        any_violation = weakness_signals.get("any_violation")
    else:
        # Component 1 fallback: derive a same-spirit violation flag from
        # what it does record (error rate + pod restarts), consistent with
        # (not identical to -- no recovery/p99-vs-median signal available
        # here) compute_weakness_signals's own error_rate/pod_restarts legs.
        errors = wrk2.get("errors", {})
        requests_total = wrk2.get("requests_total", 0) or 0
        error_count = sum(errors.get(k, 0) for k in
                           ("connect", "read", "write", "timeout", "http_non2xx3xx"))
        error_rate = (error_count / requests_total) if requests_total else 0.0
        pod_restarts = d.get("derived", {}).get("pod_restarts_during_fault", 0)
        any_violation = (error_rate > 0.05) or (pod_restarts > 0)
    return throughput, any_violation


def median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def ground_truth_for_candidate(candidate: dict, scenarios: dict) -> dict | None:
    """Amendment item 7 (2026-08-18): Component 3 injections only. Component
    1 pooling was dropped after it was found to drown out Component 3's real
    signal for overlapping candidates (see module docstring / PREREGISTRATION.md
    item 7b) -- `scenarios` is accepted for signature stability / a future
    robustness-check appendix but is not used in the primary ground truth."""
    del scenarios
    injection_files = component3_injections_for(candidate["id"])
    if not injection_files:
        return None

    throughputs, violations = [], []
    for path in injection_files:
        t, v = real_throughput_and_violation(path)
        if t is not None:
            throughputs.append(t)
        if v is not None:
            violations.append(v)

    if not throughputs:
        return None

    med_throughput = median(throughputs)
    degrade = (BASELINE_THROUGHPUT_RPS - med_throughput) / BASELINE_THROUGHPUT_RPS > DEGRADE_THRESHOLD
    weakness_present = (sum(violations) >= len(violations) / 2) if violations else None

    return {
        "n_component3_injections": len(injection_files),
        "median_throughput_rps": med_throughput,
        "throughput_direction": "degrade" if degrade else "no_meaningful_change",
        "weakness_signal_present": weakness_present,
    }


def load_hypotheses() -> list[dict]:
    samples = []
    for path in sorted(HYPOTHESES_DIR.glob("*/candidate-*/sample-*.json")):
        with open(path) as f:
            d = json.load(f)
        if not d.get("valid"):
            continue
        samples.append({
            "arm": d["metadata"]["arm"],
            "candidate_id": d["candidate"]["id"],
            "prediction": d["prediction"],
        })
    return samples


def balanced_accuracy(pairs: list[tuple[str, str]]) -> float | None:
    """pairs = [(predicted, actual), ...]. Binary task
    (degrade / no_meaningful_change); balanced accuracy = mean of per-class
    recall, undefined (None) if one class is entirely absent from `actual`."""
    by_class = defaultdict(lambda: [0, 0])  # actual_class -> [correct, total]
    for pred, actual in pairs:
        by_class[actual][1] += 1
        if pred == actual:
            by_class[actual][0] += 1
    if len(by_class) < 2:
        return None
    recalls = [correct / total for correct, total in by_class.values() if total > 0]
    return sum(recalls) / len(recalls)


def bootstrap_ci(pairs: list[tuple[str, str]], n_resamples: int = BOOTSTRAP_RESAMPLES,
                  seed: int = 42) -> tuple[float | None, float | None]:
    import random
    rng = random.Random(seed)
    n = len(pairs)
    if n == 0:
        return None, None
    scores = []
    for _ in range(n_resamples):
        resample = [pairs[rng.randrange(n)] for _ in range(n)]
        score = balanced_accuracy(resample)
        if score is not None:
            scores.append(score)
    if not scores:
        return None, None
    scores.sort()
    lo_idx = int(0.025 * len(scores))
    hi_idx = int(0.975 * len(scores))
    return scores[lo_idx], scores[min(hi_idx, len(scores) - 1)]


def main():
    parser = argparse.ArgumentParser(description="Component 4 mechanical scoring")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "analysis" / "results" / "component4-scoring.json"))
    args = parser.parse_args()

    fault_space = load_fault_space()
    scenarios = load_scenarios()

    ground_truth: dict[str, dict] = {}
    excluded = []
    for candidate in fault_space:
        gt = ground_truth_for_candidate(candidate, scenarios)
        if gt is None:
            excluded.append(candidate["id"])
        else:
            ground_truth[candidate["id"]] = gt

    print(f"Ground truth available for {len(ground_truth)}/{len(fault_space)} candidates "
          f"({len(excluded)} excluded, zero real executions: {excluded})")

    samples = load_hypotheses()
    print(f"Loaded {len(samples)} valid hypothesis-generation samples")

    # Amendment item 7e: majority ground-truth class, for the trivial
    # always-predict-majority baseline below.
    gt_counts = Counter(gt["throughput_direction"] for gt in ground_truth.values())
    majority_class = gt_counts.most_common(1)[0][0] if gt_counts else None

    results = {}
    arms = (sorted({s["arm"] for s in samples})
            + ["practitioner_heuristic", "always_predict_majority_class"])
    for arm in arms:
        pairs = []
        if arm == "practitioner_heuristic":
            for candidate in fault_space:
                cid = candidate["id"]
                if cid not in ground_truth:
                    continue
                pred = practitioner_heuristic.predict(candidate)
                pairs.append((pred["throughput_direction"], ground_truth[cid]["throughput_direction"]))
        elif arm == "always_predict_majority_class":
            for cid, gt in ground_truth.items():
                pairs.append((majority_class, gt["throughput_direction"]))
        else:
            for s in samples:
                if s["arm"] != arm or s["candidate_id"] not in ground_truth:
                    continue
                pairs.append((s["prediction"]["throughput_direction"],
                              ground_truth[s["candidate_id"]]["throughput_direction"]))

        ba = balanced_accuracy(pairs)
        ci_lo, ci_hi = bootstrap_ci(pairs) if pairs else (None, None)
        pred_counts = Counter(p for p, _ in pairs)
        degenerate = len(pred_counts) < 2
        results[arm] = {
            "n_scored": len(pairs),
            "balanced_accuracy": ba,
            "ci_95_lo": ci_lo,
            "ci_95_hi": ci_hi,
            "predicted_label_counts": dict(pred_counts),
            "degenerate_constant_predictor": degenerate,
        }
        flag = "  [CONSTANT PREDICTOR -- not genuine chance-level performance]" if degenerate else ""
        print(f"  {arm}: n={len(pairs)} balanced_accuracy={ba} 95% CI=[{ci_lo}, {ci_hi}] "
              f"predictions={dict(pred_counts)}{flag}")

    results["chance"] = {"n_scored": None, "balanced_accuracy": 0.5, "ci_95_lo": None, "ci_95_hi": None}

    output = {
        "candidates_with_ground_truth": len(ground_truth),
        "candidates_excluded": excluded,
        "total_candidates": len(fault_space),
        "results": results,
        "ground_truth": ground_truth,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nWritten to {out_path}")

    if excluded:
        print(f"\nNOTE: {len(excluded)} candidates have zero real executions yet "
              f"(Component 3 campaigns not yet complete). Re-run this script after "
              f"campaigns finish for the full 36-candidate score.", file=sys.stderr)


if __name__ == "__main__":
    main()
