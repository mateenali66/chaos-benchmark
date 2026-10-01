#!/usr/bin/env python3
"""
Tables and figures for Components 2 to 5. Reads only the result files those
components' scripts write, so nothing here recomputes a statistic:
analysis/results/component2_overhead.csv (component2_analyze.py),
component3-scoring.json (component3_analyze.py), component4-scoring.json
(scripts/score-hypotheses.py) and component5-scoring.json
(scripts/component5_evaluate.py).

Tables:
    Table 6b: Component 2 overhead decomposition (standing overhead vs fault side effects)
    Table 7: Component 3 discovery-curve AUC per arm, with Kruskal-Wallis
    Table 8: Component 4 balanced accuracy per arm and baseline
    Table 9: Component 5 detector AUC-ROC vs the static-threshold baseline

Figures:
    Fig 10: Component 3 discovery curves (median and IQR band per arm)
    Fig 11: Component 4 balanced accuracy with 95% CI, constant predictors greyed
    Fig 12: Component 5 median AUC-ROC with 95% CI per detector and baseline

Table 6b is the Component 2 result. Table 6 (tables.py, table6_overhead.tex)
holds Component 1's recovery time and pod restarts, despite its file name.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

RESULTS_DIR = Path(__file__).resolve().parent / "results"
TABLE_DIR = Path(__file__).resolve().parent / "tables"
FIG_DIR = Path(__file__).resolve().parent / "figures"
TABLE_DIR.mkdir(exist_ok=True)
FIG_DIR.mkdir(exist_ok=True)

ARM_LABELS = {
    "random": "Random", "coverage": "Coverage",
    "llm-claude": "LLM (Claude)", "llm-llama": "LLM (Llama 3)", "llm-mistral": "LLM (Mistral)",
}
DETECTOR_LABELS = {
    "ewma": "EWMA", "isolation_forest": "Isolation Forest",
    "autoencoder": "Autoencoder", "deep_svdd": "Deep SVDD",
    "static_threshold": "Static threshold (baseline)",
}

plt.rcParams.update({
    "font.family": "serif", "font.size": 10, "axes.labelsize": 11, "axes.titlesize": 12,
    "xtick.labelsize": 9, "ytick.labelsize": 9, "legend.fontsize": 9,
    "figure.dpi": 300, "savefig.dpi": 300, "savefig.bbox": "tight", "savefig.pad_inches": 0.1,
    "axes.grid": True, "grid.alpha": 0.3, "grid.linewidth": 0.5,
})


def save_fig(fig, name):
    fig.savefig(FIG_DIR / f"{name}.pdf", format="pdf")
    fig.savefig(FIG_DIR / f"{name}.png", format="png")
    print(f"  Saved: {name}.pdf / {name}.png")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Component 2
# ---------------------------------------------------------------------------

def table6b_component2():
    with open(RESULTS_DIR / "component2_overhead.csv") as f:
        rows = list(csv.DictReader(f))

    lines = [
        r"\begin{table*}[ht]", r"\centering",
        r"\caption{Component 2: effect of Chaos Mesh on the application pods' mean CPU and memory per pod (Cluster A)}",
        r"\label{tab:component2}", r"\small",
        r"\begin{tabular}{lrrrrr}", r"\hline",
        r"\textbf{Metric} & \textbf{Baseline} & \textbf{Idle} & \textbf{Fault} & \textbf{Idle $-$ baseline} & \textbf{Fault $-$ idle} \\",
        r" & (no tool) & (tool, no fault) & (tool + fault) & (idle $-$ baseline) & (fault $-$ idle) \\",
        r"\hline",
    ]
    for row in rows:
        def ci(prefix):
            lo, hi = row.get(f"{prefix}_ci_lo"), row.get(f"{prefix}_ci_hi")
            return f" [{float(lo):.4f}, {float(hi):.4f}]" if lo not in (None, "", "nan") else ""
        lines.append(
            f"{row['metric']} & {float(row['baseline_median']):.4f} & {float(row['idle_median']):.4f} & "
            f"{float(row['fault_median']):.4f} & "
            f"{float(row['standing_overhead_idle_minus_baseline']):+.4f}{ci('standing_overhead')} & "
            f"{float(row['fault_side_effect_fault_minus_idle']):+.4f}{ci('fault_side_effect')} \\\\"
        )
    n = rows[0]["n_baseline"] if rows else "?"
    lines += [
        r"\hline", r"\end{tabular}", r"\vspace{2mm}",
        rf"\raggedright\footnotesize $n={n}$ repetitions per configuration. Values are bootstrap medians "
        r"(CPU in cores, memory in MB); overhead/side-effect columns show the median difference with a "
        r"95\% percentile bootstrap CI in brackets (10{,}000 resamples, seed 42). No significance test is "
        r"registered for this component. The measure covers the application namespace only, not the tool's "
        r"own pods. LitmusChaos is not reported (main text Section 4.3).",
        r"\end{table*}",
    ]
    (TABLE_DIR / "table6b_component2.tex").write_text("\n".join(lines))
    print("  Written: table6b_component2.tex")


# ---------------------------------------------------------------------------
# Component 3
# ---------------------------------------------------------------------------

def load_component3():
    with open(RESULTS_DIR / "component3-scoring.json") as f:
        return json.load(f)


def table7_component3(data: dict):
    arms = list(ARM_LABELS.keys())
    summary = data["auc_by_arm_summary"]
    kw = data["kruskal_wallis"]
    pairwise_vs_random = {p["arm_b"] if p["arm_a"] == "random" else p["arm_a"]: p
                           for p in data["pairwise_mann_whitney"] if "random" in (p["arm_a"], p["arm_b"])}

    lines = [
        r"\begin{table*}[ht]", r"\centering",
        r"\caption{Component 3: discovery-curve AUC by fault-selection arm (10 campaigns/arm)}",
        r"\label{tab:component3}", r"\small",
        r"\begin{tabular}{lrrrrl}", r"\hline",
        r"\textbf{Arm} & \textbf{Median AUC} & \textbf{Min} & \textbf{Max} & \textbf{$p$ vs Random (Holm)} & \textbf{Sig.} \\",
        r"\hline",
    ]
    for arm in arms:
        s = summary[arm]
        vals = s["values"]
        vs_random = "--" if arm == "random" else f"{pairwise_vs_random[arm]['p_adjusted_holm']:.4f}"
        sig = "" if arm == "random" else ("$^*$" if pairwise_vs_random[arm]["significant_after_holm"] else "")
        lines.append(
            f"{ARM_LABELS[arm]} & {s['median']:.1f} & {min(vals):.1f} & {max(vals):.1f} & {vs_random} & {sig} \\\\"
        )
    lines += [
        r"\hline", r"\end{tabular}", r"\vspace{2mm}",
        rf"\raggedright\footnotesize Kruskal-Wallis across all 5 arms: $H={kw['statistic']:.2f}$, "
        rf"$p={kw['p_value']:.5f}$. Pairwise $p$-values are two-sided Mann-Whitney U, Holm-corrected across "
        r"all $\binom{5}{2}=10$ pairs (the full pairwise matrix, not only vs Random shown here, is in the "
        r"reproducibility package, Section~\ref{sec:conclusions}). $^*$Significant at $\alpha=0.05$.",
        r"\end{table*}",
    ]
    (TABLE_DIR / "table7_component3.tex").write_text("\n".join(lines))
    print("  Written: table7_component3.tex")


def fig10_discovery_curves(data: dict):
    fig, ax = plt.subplots(figsize=(8, 5))
    colors = plt.cm.tab10(np.linspace(0, 1, len(ARM_LABELS)))
    for (arm, label), color in zip(ARM_LABELS.items(), colors):
        campaigns = [r for r in data["per_campaign"] if r["arm"] == arm]
        curves = np.array([c["discovery_curve"] for c in campaigns])  # (10, 10)
        median_curve = np.median(curves, axis=0)
        q1 = np.percentile(curves, 25, axis=0)
        q3 = np.percentile(curves, 75, axis=0)
        x = np.arange(1, 11)
        ax.plot(x, median_curve, label=label, color=color, linewidth=1.8)
        ax.fill_between(x, q1, q3, color=color, alpha=0.12)
    ax.set_xlabel("Injection index within campaign")
    ax.set_ylabel("Cumulative unique weakness classes")
    ax.set_title("Component 3: discovery curves by fault-selection arm (median, IQR band)")
    ax.set_xticks(range(1, 11))
    ax.legend(loc="upper left")
    fig.tight_layout()
    save_fig(fig, "fig10_discovery_curves")


# ---------------------------------------------------------------------------
# Component 4
# ---------------------------------------------------------------------------

def load_component4():
    with open(RESULTS_DIR / "component4-scoring.json") as f:
        return json.load(f)


ARM4_ORDER = ["llm-claude", "llm-llama", "llm-mistral", "practitioner_heuristic",
              "always_predict_majority_class", "chance"]
ARM4_LABELS = {
    "llm-claude": "LLM (Claude)", "llm-llama": "LLM (Llama 3)", "llm-mistral": "LLM (Mistral)",
    "practitioner_heuristic": "Practitioner heuristic", "always_predict_majority_class": "Always-majority baseline",
    "chance": "Chance",
}


def table8_component4(data: dict):
    results = data["results"]
    lines = [
        r"\begin{table*}[ht]", r"\centering",
        r"\caption{Component 4: balanced accuracy of throughput-direction prediction (36 candidates)}",
        r"\label{tab:component4}", r"\small",
        r"\begin{tabular}{lrrrl}", r"\hline",
        r"\textbf{Arm} & \textbf{$n$} & \textbf{Balanced Acc.} & \textbf{95\% CI} & \textbf{Note} \\",
        r"\hline",
    ]
    for arm in ARM4_ORDER:
        r = results.get(arm)
        if not r:
            continue
        n = r["n_scored"] if r["n_scored"] is not None else "--"
        ba = f"{r['balanced_accuracy']:.3f}" if r["balanced_accuracy"] is not None else "--"
        ci = (f"[{r['ci_95_lo']:.3f}, {r['ci_95_hi']:.3f}]"
              if r.get("ci_95_lo") is not None else "--")
        degenerate = r.get("predicted_label_counts") and len(r["predicted_label_counts"]) < 2
        note = "constant predictor" if degenerate else ""
        lines.append(f"{ARM4_LABELS[arm]} & {n} & {ba} & {ci} & {note} \\\\")
    lines += [
        r"\hline", r"\end{tabular}", r"\vspace{2mm}",
        r"\raggedright\footnotesize 95\% CIs are percentile bootstrap (10{,}000 resamples, seed 42). "
        r"``Constant predictor'' means the arm predicted the same label for every sample in its scored set, "
        r"which mechanically produces balanced accuracy $=0.5$ with zero-width CI. That differs from "
        r"chance-level performance with varying predictions (Section~\ref{sec:meth-comp4}).",
        r"\end{table*}",
    ]
    (TABLE_DIR / "table8_component4.tex").write_text("\n".join(lines))
    print("  Written: table8_component4.tex")


def fig11_component4_bars(data: dict):
    results = data["results"]
    arms = [a for a in ARM4_ORDER if a in results and a != "chance"]
    medians = [results[a]["balanced_accuracy"] for a in arms]
    lo = [results[a].get("ci_95_lo") for a in arms]
    hi = [results[a].get("ci_95_hi") for a in arms]
    degenerate = [bool(results[a].get("predicted_label_counts")) and
                  len(results[a]["predicted_label_counts"]) < 2 for a in arms]

    fig, ax = plt.subplots(figsize=(8, 5))
    x = np.arange(len(arms))
    yerr = np.array([[m - l if l is not None else 0 for m, l in zip(medians, lo)],
                      [h - m if h is not None else 0 for m, h in zip(medians, hi)]])
    colors = ["#B0BEC5" if d else "#2196F3" for d in degenerate]
    ax.bar(x, medians, color=colors, yerr=yerr, capsize=4, error_kw={"linewidth": 0.9})
    ax.axhline(0.5, color="gray", linestyle="--", linewidth=1, label="Chance (0.5)")
    ax.set_xticks(x)
    ax.set_xticklabels([ARM4_LABELS[a] for a in arms], rotation=30, ha="right")
    ax.set_ylabel("Balanced accuracy")
    ax.set_title("Component 4: throughput-direction prediction vs chance")
    ax.set_ylim(0, 1)
    ax.legend(loc="upper right")
    fig.tight_layout()
    save_fig(fig, "fig11_component4_balanced_accuracy")


# ---------------------------------------------------------------------------
# Component 5
# ---------------------------------------------------------------------------

def load_component5():
    # Slot-0 runs only: the other runs' sidecars hold slot 0's telemetry
    # (analysis/component5_slot0.py, PREREGISTRATION.md erratum).
    with open(RESULTS_DIR / "component5-slot0.json") as f:
        return json.load(f)


def _p_tex(p: float) -> str:
    """p-value for a table cell: three decimals, or a power of ten below 0.001."""
    if p >= 0.001:
        return f"{p:.3f}"
    mant, exp = f"{p:.2e}".split("e")
    return rf"${mant}\times10^{{{int(exp)}}}$"


DETECTOR5_ORDER = ["isolation_forest", "autoencoder", "deep_svdd", "ewma", "static_threshold"]


def table9_component5(data: dict):
    summary = data["summary"]
    confirmatory = data["confirmatory_vs_static_threshold"]
    n_runs = data["n_runs_scored"]
    base = summary["static_threshold"]
    n_paired = base["n_scored"]
    n_degen = base["n_degenerate_excluded"]
    lines = [
        r"\begin{table*}[pos=t]", r"\centering",
        r"\caption{Component 5: detector AUC-ROC vs the static-threshold baseline (slot-0 runs: scenarios P1, P2, P3, N1)}",
        r"\label{tab:component5}", r"\small",
        r"\resizebox{\textwidth}{!}{%",
        r"\begin{tabular}{lrrrrl}", r"\hline",
        rf"\textbf{{Detector}} & \textbf{{$n$ (full)}} & \textbf{{Median AUC (full)}} & \textbf{{Median AUC ($n={n_paired}$ paired)}} & \textbf{{95\% CI (full)}} & \textbf{{vs baseline ($p_{{\mathrm{{Holm}}}}$)}} \\",
        r"\hline",
    ]
    for name in DETECTOR5_ORDER:
        s = summary[name]
        med = f"{s['median_auc']:.3f}"
        ci = f"[{s['ci_95_lo']:.3f}, {s['ci_95_hi']:.3f}]"
        if name == "static_threshold":
            paired_med = med  # the baseline's scored runs are the paired subset
            vs_base = f"({n_degen} degenerate runs excluded)"
        else:
            c = confirmatory[name]
            paired_med = f"{c['median_auc_paired_subset']:.3f}"
            vs_base = _p_tex(c["p_adjusted_holm"]) + ("$^*$" if c["significant_after_holm"] else "")
        lines.append(f"{DETECTOR_LABELS[name]} & {s['n_scored']} & {med} & {paired_med} & {ci} & {vs_base} \\\\")
    lines += [
        r"\hline", r"\end{tabular}%", r"}", r"\par\vspace{2mm}",
        r"\noindent\begin{minipage}{\linewidth}\raggedright\footnotesize "
        rf"`Full' = all {n_runs} scored slot-0 runs for the detectors, and the {n_paired} runs where the baseline "
        rf"is not constant (it gives one score for the whole run on the other {n_degen}, see main text). "
        rf"`$n={n_paired}$ paired' = each detector's median AUC on those {n_paired} runs, comparable with the "
        rf"baseline's {base['median_auc']:.3f}. 95\% CIs are percentile bootstrap on the per-run median AUC "
        r"(10{,}000 resamples, seed 42). $p_{\mathrm{Holm}}$: two-sided paired Wilcoxon signed-rank against the "
        rf"baseline on the {n_paired} paired runs, Holm-corrected across the 4 detectors. $^*$Significant at "
        r"$\alpha=0.05$.\end{minipage}",
        r"\end{table*}",
    ]
    (TABLE_DIR / "table9_component5.tex").write_text("\n".join(lines))
    print("  Written: table9_component5.tex")


def fig12_component5_bars(data: dict):
    summary = data["summary"]
    names = DETECTOR5_ORDER
    medians = [summary[n]["median_auc"] for n in names]
    lo = [summary[n].get("ci_95_lo") for n in names]
    hi = [summary[n].get("ci_95_hi") for n in names]

    # One cas-dc column (\columnwidth = 238.25pt = 3.31in): figsize makes the
    # tight-bbox PDF ~3.3in wide, so these point sizes are the printed sizes.
    # rc_context leaves the module-wide style of fig10 and fig11 unchanged.
    with plt.rc_context({"font.size": 7.5, "axes.labelsize": 8, "xtick.labelsize": 7.5,
                         "ytick.labelsize": 7.5, "axes.linewidth": 0.8}):
        fig, ax = plt.subplots(figsize=(3.25, 2.6))
        x = np.arange(len(names))
        yerr = np.array([[m - l if l is not None else 0 for m, l in zip(medians, lo)],
                          [h - m if h is not None else 0 for m, h in zip(medians, hi)]])
        colors = ["#FF9800" if n == "static_threshold" else "#2196F3" for n in names]
        ax.bar(x, medians, color=colors, yerr=yerr, capsize=4, error_kw={"linewidth": 0.9})
        ax.axhline(0.5, color="gray", linestyle="--", linewidth=1)
        # Direct label just outside the right spine instead of a legend:
        # every bar crosses 0.5, so the line has no bar-free spot inside the
        # axes.
        ax.annotate("Chance\n(0.5)", xy=(1.0, 0.5), xycoords=("axes fraction", "data"),
                    xytext=(3, 0), textcoords="offset points", ha="left", va="center",
                    fontsize=7, color="dimgray", linespacing=1.0, annotation_clip=False)
        ax.set_xticks(x)
        ax.set_xticklabels([DETECTOR_LABELS[n].replace(" (baseline)", "\n(baseline)") for n in names],
                           rotation=30, ha="right", rotation_mode="anchor", linespacing=1.0)
        ax.set_ylabel("Median AUC-ROC (per-run)")
        # No in-figure title. The LaTeX caption carries it.
        ax.set_ylim(0, 1)
        ax.set_axisbelow(True)  # grid behind the bars, not drawn through them
        fig.tight_layout()
        save_fig(fig, "fig12_component5_auc")


def main():
    print("Component 2...")
    table6b_component2()

    print("Component 3...")
    d3 = load_component3()
    table7_component3(d3)
    fig10_discovery_curves(d3)

    print("Component 4...")
    d4 = load_component4()
    table8_component4(d4)
    fig11_component4_bars(d4)

    print("Component 5...")
    d5 = load_component5()
    table9_component5(d5)
    fig12_component5_bars(d5)

    print(f"\nAll tables saved to {TABLE_DIR}/, figures to {FIG_DIR}/")


if __name__ == "__main__":
    main()
