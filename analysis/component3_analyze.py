#!/usr/bin/env python3
"""
Component 3: fault-selection strategy study (analysis/PREREGISTRATION.md,
"Metrics" and "Confirmatory analyses" #5).

For each of the 50 campaigns (5 arms x 10 campaigns, K=10 injections each),
builds the discovery curve: the cumulative count of unique weakness classes
after each injection, in the order the injections ran. A weakness class is
a (signal, target_service) pair. The signals are error_rate_violation,
p99_over_3x_baseline, recovery_over_60s and pod_restarts_violation
(compute_weakness_signals in scripts/run-campaign.py). A class counts once
per campaign, at the injection where it first appears. Errored injections
add nothing.

Primary metric: discovery-curve AUC, the trapezoidal area under the
cumulative count against injection index 1..10. Secondary: total unique
classes at K=10 (the curve's final value) and injections to the first
weakness (None if there is none).

Confirmatory analysis #5: Kruskal-Wallis across the five arms on
discovery-curve AUC (10 campaigns per arm). Campaigns are independent
because each starts with an empty history. If p < 0.05, pairwise two-sided
Mann-Whitney U with Holm correction, and Cliff's delta with a 95% BCa
bootstrap CI (percentile fallback where BCa is undefined, as in
analyze.py).

p99_over_3x_baseline is None until the campaign has an earlier p99 value to
form a running median, so always on its first injection. None counts as not
violated: an unmeasurable signal cannot be discovered.

Usage:
    python3 analysis/component3_analyze.py
    python3 analysis/component3_analyze.py --exclude-signal recovery_over_60s \
        --out analysis/results/component3-scoring-no-recovery.json

--exclude-signal drops one signal from the weakness classes (post hoc
sensitivity analysis, not in the plan). It writes only the JSON given by
--out and leaves the default outputs untouched.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.stats import kruskal, mannwhitneyu, bootstrap

CAMPAIGNS_DIR = Path(__file__).resolve().parent.parent / "data-v2" / "ml" / "campaigns"
OUTPUT_DIR = Path(__file__).resolve().parent / "results"
OUTPUT_DIR.mkdir(exist_ok=True)

ARMS = ["random", "coverage", "llm-claude", "llm-llama", "llm-mistral"]
K = 10
SIGNAL_FIELDS = {
    "error_rate_violation": "error_rate",
    "p99_over_3x_baseline": "p99",
    "recovery_over_60s": "recovery",
    "pod_restarts_violation": "pod_restarts",
}
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 42


def load_campaign_injections(arm: str, campaign: int) -> list[dict] | None:
    campaign_dir = CAMPAIGNS_DIR / arm / f"campaign-{campaign}"
    injections = []
    for i in range(1, K + 1):
        path = campaign_dir / f"injection-{i}.json"
        if not path.exists():
            return None
        with open(path) as f:
            injections.append(json.load(f))
    return injections


def discovery_curve(injections: list[dict], fields: dict[str, str] = SIGNAL_FIELDS) -> list[int]:
    """Returns cumulative unique-weakness-class counts, one value per
    injection index 1..K, in the order injections actually ran."""
    seen: set[tuple[str, str]] = set()
    curve = []
    for rec in injections:
        ws = rec.get("weakness_signals") or {}
        target = rec["metadata"]["target_service"]
        if rec.get("error") is None:  # errored injections contribute no signal
            for field, signal_name in fields.items():
                if ws.get(field) is True:  # explicit True; None/False both non-events
                    seen.add((signal_name, target))
        curve.append(len(seen))
    return curve


def trapezoidal_auc(curve: list[int]) -> float:
    """Area under the discovery curve over x = injection index 1..K."""
    x = np.arange(1, len(curve) + 1)
    return float(np.trapezoid(curve, x))


def cliffs_delta(x: list[float], y: list[float]) -> tuple[float, str]:
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    more = (x[:, None] > y[None, :]).sum()
    less = (x[:, None] < y[None, :]).sum()
    delta = (more - less) / (len(x) * len(y))
    a = abs(delta)
    mag = "negligible" if a < 0.147 else "small" if a < 0.33 else "medium" if a < 0.474 else "large"
    return float(delta), mag


def _cliffs_stat(x, y):
    more = (x[:, None] > y[None, :]).sum()
    less = (x[:, None] < y[None, :]).sum()
    return (more - less) / (len(x) * len(y))


def cliffs_delta_ci(x: list[float], y: list[float]) -> tuple[float, float]:
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    try:
        res = bootstrap((x, y), _cliffs_stat, n_resamples=BOOTSTRAP_RESAMPLES, paired=False,
                         vectorized=False, method="BCa", random_state=rng, confidence_level=0.95)
        lo, hi = float(res.confidence_interval.low), float(res.confidence_interval.high)
        if lo == lo and hi == hi:
            return lo, hi
    except Exception:
        pass
    scores = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        xs = rng.choice(x, size=len(x), replace=True)
        ys = rng.choice(y, size=len(y), replace=True)
        scores.append(_cliffs_stat(xs, ys))
    scores.sort()
    return float(scores[int(0.025 * len(scores))]), float(scores[int(0.975 * len(scores)) - 1])


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
    parser = argparse.ArgumentParser(description="Component 3 discovery-curve analysis")
    parser.add_argument("--exclude-signal", choices=sorted(SIGNAL_FIELDS),
                        help="drop this signal from the weakness classes (post hoc sensitivity analysis)")
    parser.add_argument("--out", type=Path, help="output JSON (required with --exclude-signal)")
    args = parser.parse_args()
    if args.exclude_signal and not args.out:
        parser.error("--out is required with --exclude-signal")
    fields = {k: v for k, v in SIGNAL_FIELDS.items() if k != args.exclude_signal}

    per_campaign_rows = []
    auc_by_arm: dict[str, list[float]] = defaultdict(list)

    for arm in ARMS:
        for campaign in range(1, 11):
            injections = load_campaign_injections(arm, campaign)
            if injections is None:
                print(f"  WARNING: {arm}/campaign-{campaign} incomplete, skipping")
                continue
            curve = discovery_curve(injections, fields)
            auc = trapezoidal_auc(curve)
            total_at_k = curve[-1]
            first_idx = next((i + 1 for i, c in enumerate(curve) if c > 0), None)
            auc_by_arm[arm].append(auc)
            per_campaign_rows.append({
                "arm": arm, "campaign": campaign,
                "discovery_curve": curve, "auc": auc,
                "total_unique_at_k10": total_at_k,
                "injections_to_first_weakness": first_idx,
            })

    print("Per-arm discovery-curve AUC (n=10 campaigns each):")
    for arm in ARMS:
        vals = auc_by_arm[arm]
        print(f"  {arm}: n={len(vals)} median_AUC={np.median(vals) if vals else None} "
              f"(min={min(vals) if vals else None}, max={max(vals) if vals else None})")

    # Confirmatory analysis #5: Kruskal-Wallis across the five arms.
    groups = [auc_by_arm[arm] for arm in ARMS if len(auc_by_arm[arm]) >= 2]
    kw_result = {}
    pairwise = []
    if len(groups) >= 2:
        stat, p = kruskal(*groups)
        kw_result = {"statistic": float(stat), "p_value": float(p), "n_arms": len(groups)}
        print(f"\nKruskal-Wallis across {len(groups)} arms: H={stat:.4f}, p={p:.5f}")

        if p < 0.05:
            print("  p < 0.05: running pairwise Mann-Whitney U with Holm correction...")
            pairs = [(a, b) for i, a in enumerate(ARMS) for b in ARMS[i + 1:]]
            p_values = []
            for a, b in pairs:
                xa, xb = auc_by_arm[a], auc_by_arm[b]
                if len(xa) < 2 or len(xb) < 2:
                    continue
                u_stat, u_p = mannwhitneyu(xa, xb, alternative="two-sided")
                delta, mag = cliffs_delta(xa, xb)
                ci_lo, ci_hi = cliffs_delta_ci(xa, xb)
                pairwise.append({
                    "arm_a": a, "arm_b": b, "median_a": float(np.median(xa)), "median_b": float(np.median(xb)),
                    "U_statistic": float(u_stat), "p_value": float(u_p),
                    "cliffs_delta": delta, "effect_size": mag,
                    "delta_ci_95_lo": ci_lo, "delta_ci_95_hi": ci_hi,
                })
                p_values.append(float(u_p))
            adjusted, significant = holm_bonferroni(p_values)
            for row, adj, sig in zip(pairwise, adjusted, significant):
                row["p_adjusted_holm"] = adj
                row["significant_after_holm"] = sig
            print("  Pairwise results (Holm-corrected):")
            for row in pairwise:
                print(f"    {row['arm_a']} vs {row['arm_b']}: median {row['median_a']:.1f} vs "
                      f"{row['median_b']:.1f}, delta={row['cliffs_delta']:+.3f} ({row['effect_size']}), "
                      f"p_holm={row['p_adjusted_holm']:.4f}, sig={row['significant_after_holm']}")
        else:
            print("  p >= 0.05: no pairwise tests run (per PREREGISTRATION.md's confirmatory analysis #5).")

    # Write outputs. The sensitivity run writes only its own JSON.
    if not args.exclude_signal:
        with open(OUTPUT_DIR / "component3_per_campaign.csv", "w", newline="") as f:
            fieldnames = ["arm", "campaign", "auc", "total_unique_at_k10", "injections_to_first_weakness"]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in per_campaign_rows:
                writer.writerow({k: row[k] for k in fieldnames})
        print(f"\nWritten: {OUTPUT_DIR / 'component3_per_campaign.csv'}")

    output = {
        "per_campaign": per_campaign_rows,
        "auc_by_arm_summary": {
            arm: {
                "n": len(auc_by_arm[arm]),
                "median": float(np.median(auc_by_arm[arm])) if auc_by_arm[arm] else None,
                "values": auc_by_arm[arm],
            } for arm in ARMS
        },
        "kruskal_wallis": kw_result,
        "pairwise_mann_whitney": pairwise,
    }
    if args.exclude_signal:
        output = {"post_hoc": True, "excluded_signal": args.exclude_signal, **output}
    out_path = args.out or OUTPUT_DIR / "component3-scoring.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"Written: {out_path}")


if __name__ == "__main__":
    main()
