#!/usr/bin/env python3
"""Component 5 post hoc analyses reported in the manuscript.

Reads analysis/results/component5-scoring.json (written by
scripts/component5_evaluate.py) and computes two things that are not part of
the pre-registered confirmatory test:

1. Sensitivity: every run where the static-threshold baseline is degenerate
   (constant score) is scored as AUC = 0.5 instead of excluded. Each detector
   is then compared with the baseline over all scored runs by a two-sided
   Wilcoxon signed-rank test, Holm-corrected across the 4 detectors.
2. Descriptive: each detector's median AUC on the runs where the baseline is
   degenerate versus the runs where it is not.

Usage: python analysis/component5_sensitivity.py [--in PATH] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from scipy.stats import wilcoxon

ROOT = Path(__file__).resolve().parent
DETECTORS = ["isolation_forest", "autoencoder", "deep_svdd", "ewma"]
BASE = "static_threshold"


def holm(pvals: list[float]) -> list[float]:
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    adj = [0.0] * m
    running = 0.0
    for rank, idx in enumerate(order):
        running = max(running, min((m - rank) * pvals[idx], 1.0))
        adj[idx] = running
    return adj


def sensitivity(runs: list[dict]) -> dict:
    raw, rows = [], []
    for name in DETECTORS:
        pairs = [(r["auc"][name], 0.5 if r["auc"][BASE] is None else r["auc"][BASE])
                 for r in runs if r["auc"][name] is not None]
        det = np.array([p[0] for p in pairs])
        base = np.array([p[1] for p in pairs])
        _, p = wilcoxon(det, base, alternative="two-sided")
        raw.append(float(p))
        rows.append({
            "detector": name,
            "n": len(pairs),
            "median_auc_detector": float(np.median(det)),
            "median_auc_baseline": float(np.median(base)),
            "median_paired_difference": float(np.median(det - base)),
            "p_value": float(p),
        })
    for row, adj in zip(rows, holm(raw)):
        row["p_adjusted_holm"] = adj
    return {"degenerate_baseline_scored_as": 0.5, "results": rows}


def degenerate_split(runs: list[dict]) -> dict:
    degen = [r for r in runs if r["degenerate"][BASE]]
    nondeg = [r for r in runs if not r["degenerate"][BASE]]
    out = {"n_degenerate": len(degen), "n_non_degenerate": len(nondeg), "results": []}
    for name in DETECTORS:
        out["results"].append({
            "detector": name,
            "median_auc_degenerate_baseline_runs":
                float(np.median([r["auc"][name] for r in degen if r["auc"][name] is not None])),
            "median_auc_non_degenerate_baseline_runs":
                float(np.median([r["auc"][name] for r in nondeg if r["auc"][name] is not None])),
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--in", dest="inp", type=Path, default=ROOT / "results" / "component5-scoring.json")
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "component5-sensitivity.json")
    args = ap.parse_args()

    runs = json.loads(args.inp.read_text())["per_run"]
    result = {
        "source": str(args.inp.relative_to(ROOT.parent)) if args.inp.is_relative_to(ROOT.parent) else str(args.inp),
        "post_hoc": True,
        "sensitivity_degenerate_as_0_5": sensitivity(runs),
        "detector_medians_by_baseline_degeneracy": degenerate_split(runs),
    }
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    for row in result["sensitivity_degenerate_as_0_5"]["results"]:
        print(f"{row['detector']:17s} n={row['n']} median={row['median_auc_detector']:.3f} "
              f"p_holm={row['p_adjusted_holm']:.3g}")
    split = result["detector_medians_by_baseline_degeneracy"]
    for row in split["results"]:
        print(f"{row['detector']:17s} degenerate({split['n_degenerate']})="
              f"{row['median_auc_degenerate_baseline_runs']:.3f} "
              f"non-degenerate({split['n_non_degenerate']})="
              f"{row['median_auc_non_degenerate_baseline_runs']:.3f}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
