#!/usr/bin/env python3
"""
Component 5 evaluation harness (analysis/PREREGISTRATION.md, Component 5).

For each Component 1 run: extract features (component5_features.py), fit
each of the 4 frozen detectors plus the static-threshold baseline ONLY on
that run's own baseline phase (component5_detectors.py), score the whole
run, and compute one AUC-ROC per (run, scorer) -- the unit of analysis
throughout is PER RUN, giving a paired design across the 720 runs for the
confirmatory comparison.

Primary metric: AUC-ROC per detector, median/IQR + 95% percentile
bootstrap CI across runs (10,000 resamples, seed 42 -- explicitly
registered here; PREREGISTRATION.md's Component 5 text does not specify a
method/resample count, same gap category already found and fixed for
Component 4, disclosed rather than silently assumed).

Confirmatory: each detector vs static_threshold, two-sided Wilcoxon
signed-rank on the per-run AUC differences (paired: both scorers see the
identical run, so this is a legitimate paired test), Holm-Bonferroni
correction across the 4 detectors. Runs where a scorer's score is constant
(numpy std < 1e-12, e.g. the static-threshold baseline finding zero
resource-usage violations on a pod-failure fault -- see
component5_detectors.py) are EXCLUDED from that scorer's AUC list, flagged
in the output, and never silently treated as a genuine AUC=0.5.

Usage:
    python3 scripts/component5_evaluate.py [--limit N] [--out analysis/results/component5-scoring.json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
from scipy.stats import wilcoxon

sys.path.insert(0, str(Path(__file__).resolve().parent))
from component5_features import find_run_pairs, load_run_features  # noqa: E402
from component5_detectors import DETECTORS, BASELINE_NAME, static_threshold_baseline  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 42


def safe_auc(label: np.ndarray, scores: np.ndarray) -> tuple[float | None, bool]:
    """Returns (auc, degenerate). degenerate=True means the scorer produced
    a constant score for this run -- sklearn's roc_auc_score would silently
    return exactly 0.5 for this case, indistinguishable in the number alone
    from genuine chance-level discrimination (the same failure mode found
    and fixed in Component 4's scoring)."""
    from sklearn.metrics import roc_auc_score
    if np.std(scores) < 1e-12:
        return None, True
    if label.sum() == 0 or (~label).sum() == 0:
        return None, True  # only one class present, AUC undefined
    return float(roc_auc_score(label, scores)), False


def evaluate_run(run_json: Path, ts_path: Path) -> dict | None:
    feats = load_run_features(run_json, ts_path)
    if feats is None:
        return None
    X, mask, label, names = feats["X"], feats["baseline_mask"], feats["label"], feats["feature_names"]

    result = {"metadata": feats["metadata"], "auc": {}, "degenerate": {}}
    for name, fn in DETECTORS.items():
        try:
            scores = fn(X, mask, names)
            auc, degenerate = safe_auc(label, scores)
        except Exception as e:  # noqa: BLE001 -- one run's numerical failure must not kill the batch
            auc, degenerate = None, True
            result.setdefault("errors", {})[name] = str(e)
        result["auc"][name] = auc
        result["degenerate"][name] = degenerate

    scores = static_threshold_baseline(X, mask, names)
    auc, degenerate = safe_auc(label, scores)
    result["auc"][BASELINE_NAME] = auc
    result["degenerate"][BASELINE_NAME] = degenerate
    return result


def bootstrap_ci(values: list[float], n_resamples: int = BOOTSTRAP_RESAMPLES,
                  seed: int = BOOTSTRAP_SEED) -> tuple[float, float]:
    if len(values) < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    arr = np.asarray(values)
    resampled_medians = np.median(rng.choice(arr, size=(n_resamples, len(arr)), replace=True), axis=1)
    return float(np.percentile(resampled_medians, 2.5)), float(np.percentile(resampled_medians, 97.5))


def holm_bonferroni(p_values: list[float], alpha: float = 0.05) -> tuple[list[float], list[bool]]:
    m = len(p_values)
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [0.0] * m
    running_max = 0.0
    for rank, idx in enumerate(order):
        adj = min((m - rank) * p_values[idx], 1.0)
        running_max = max(running_max, adj)
        adjusted[idx] = running_max
    return adjusted, [a < alpha for a in adjusted]


def main():
    parser = argparse.ArgumentParser(description="Component 5 detector evaluation")
    parser.add_argument("--limit", type=int, default=None, help="only process the first N run pairs (smoke testing)")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "analysis" / "results" / "component5-scoring.json"))
    args = parser.parse_args()

    pairs = list(find_run_pairs())
    if args.limit:
        pairs = pairs[: args.limit]
    print(f"Evaluating {len(pairs)} Component 1 runs...")

    per_run_results = []
    t0 = time.time()
    skipped = 0
    for i, (run_json, ts_path) in enumerate(pairs):
        r = evaluate_run(run_json, ts_path)
        if r is None:
            skipped += 1
            continue
        per_run_results.append(r)
        if (i + 1) % 50 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed
            eta = (len(pairs) - i - 1) / rate
            print(f"  {i + 1}/{len(pairs)} done ({elapsed:.0f}s elapsed, ETA {eta:.0f}s), "
                  f"{skipped} skipped so far")

    print(f"Done: {len(per_run_results)} runs scored, {skipped} skipped (missing phases/insufficient data)")

    scorer_names = list(DETECTORS.keys()) + [BASELINE_NAME]
    summary = {}
    for name in scorer_names:
        aucs = [r["auc"][name] for r in per_run_results if r["auc"][name] is not None]
        n_degenerate = sum(1 for r in per_run_results if r["degenerate"][name])
        med = float(np.median(aucs)) if aucs else None
        q1, q3 = (float(np.percentile(aucs, 25)), float(np.percentile(aucs, 75))) if aucs else (None, None)
        ci_lo, ci_hi = bootstrap_ci(aucs) if len(aucs) >= 2 else (None, None)
        summary[name] = {
            "n_scored": len(aucs),
            "n_degenerate_excluded": n_degenerate,
            "median_auc": med, "q1_auc": q1, "q3_auc": q3,
            "ci_95_lo": ci_lo, "ci_95_hi": ci_hi,
        }
        print(f"  {name}: n={len(aucs)} (excluded {n_degenerate} degenerate) "
              f"median_AUC={med} 95% CI=[{ci_lo}, {ci_hi}]")

    # Confirmatory: each detector vs static_threshold, paired Wilcoxon
    # signed-rank on runs where BOTH scorers produced a genuine (non-
    # degenerate) AUC, Holm-Bonferroni across the 4 detectors.
    confirmatory = {}
    p_values = []
    detector_names = list(DETECTORS.keys())
    for name in detector_names:
        paired = [(r["auc"][name], r["auc"][BASELINE_NAME]) for r in per_run_results
                  if r["auc"][name] is not None and r["auc"][BASELINE_NAME] is not None]
        if len(paired) < 10:
            confirmatory[name] = {"n_paired": len(paired), "p_value": None, "note": "too few paired runs"}
            p_values.append(1.0)
            continue
        det_aucs = np.array([p[0] for p in paired])
        base_aucs = np.array([p[1] for p in paired])
        diffs = det_aucs - base_aucs
        if np.all(diffs == 0):
            stat, p = float("nan"), 1.0
        else:
            stat, p = wilcoxon(det_aucs, base_aucs, alternative="two-sided")
        confirmatory[name] = {
            "n_paired": len(paired),
            # Paired-subset descriptive medians, added 2026-08-18 after a
            # peer-review pass flagged that reporting the full-719-run
            # detector median alongside the baseline's 299-run median in
            # the same table cell mixes two different samples. These are
            # each detector's own median AUC computed on exactly the same
            # 299 runs the paired test uses, directly comparable to the
            # baseline's paired-subset median.
            "median_auc_paired_subset": float(np.median(det_aucs)),
            "baseline_median_auc_paired_subset": float(np.median(base_aucs)),
            "median_diff": float(np.median(diffs)),
            "wilcoxon_statistic": float(stat) if stat == stat else None,
            "p_value": float(p),
        }
        p_values.append(float(p))

    adjusted, significant = holm_bonferroni(p_values)
    for name, adj, sig in zip(detector_names, adjusted, significant):
        confirmatory[name]["p_adjusted_holm"] = adj
        confirmatory[name]["significant_after_holm"] = sig

    print("\nConfirmatory: detector vs static_threshold baseline (paired Wilcoxon, Holm-corrected)")
    for name in detector_names:
        c = confirmatory[name]
        print(f"  {name}: n_paired={c['n_paired']} median_diff={c.get('median_diff')} "
              f"p_holm={c.get('p_adjusted_holm')} significant={c.get('significant_after_holm')}")

    output = {
        "n_runs_scored": len(per_run_results),
        "n_runs_skipped": skipped,
        "summary": summary,
        "confirmatory_vs_static_threshold": confirmatory,
        "per_run": per_run_results,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nWritten to {out_path}")


if __name__ == "__main__":
    main()
