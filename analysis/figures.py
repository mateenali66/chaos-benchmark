#!/usr/bin/env python3
"""
Generate publication-quality figures for Paper 4.

Outputs to analysis/figures/ as both PDF (vector) and PNG (300 DPI).

Figures:
    Fig 3: Throughput comparison box plots by scenario and tool
    Fig 4: Latency p99 comparison bar chart
    Fig 5: Error rate comparison grouped bar chart
    Fig 6: CPU and memory overhead heatmap
    Fig 7: Throughput by fault category (grouped box plots)
"""

import json
import glob
import os
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import seaborn as sns
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATA_DIR = Path(__file__).parent.parent / "data"
FIG_DIR = Path(__file__).parent / "figures"
FIG_DIR.mkdir(exist_ok=True)

SCENARIO_NAMES = {
    "p1": "Pod Kill",
    "p2": "Container Kill",
    "p3": "Pod Failure",
    "n1": "Latency 50ms",
    "n2": "Latency 100ms",
    "n3": "Latency 300ms",
    "n4": "Packet Loss 5%",
    "n5": "Net Partition",
    "r1": "CPU Stress 80%",
    "r2": "Mem Pressure 80%",
    "a1": "HTTP Abort 503",
    "a2": "gRPC Unavailable",
}

SCENARIO_ORDER = ["p1", "p2", "p3", "n1", "n2", "n3", "n4", "n5", "r1", "r2", "a1", "a2"]

CATEGORY_MAP = {
    "p1": "Pod/Container", "p2": "Pod/Container", "p3": "Pod/Container",
    "n1": "Network", "n2": "Network", "n3": "Network", "n4": "Network", "n5": "Network",
    "r1": "Resource", "r2": "Resource",
    "a1": "Application", "a2": "Application",
}

CATEGORY_ORDER = ["Pod/Container", "Network", "Resource", "Application"]

TOOL_LABELS = {"chaos-mesh": "Chaos Mesh", "litmus": "LitmusChaos"}
TOOL_COLORS = {"Chaos Mesh": "#2196F3", "LitmusChaos": "#FF9800"}
TOOL_HATCHES = {"Chaos Mesh": "", "LitmusChaos": "//"}

# Publication style
plt.rcParams.update({
    "font.family": "serif",
    "font.size": 10,
    "axes.labelsize": 11,
    "axes.titlesize": 12,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
    "legend.fontsize": 9,
    "figure.dpi": 300,
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.1,
    "axes.grid": True,
    "grid.alpha": 0.3,
    "grid.linewidth": 0.5,
})


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_data():
    records = []
    for fpath in sorted(glob.glob(str(DATA_DIR / "*" / "*" / "run-*.json"))):
        with open(fpath) as f:
            d = json.load(f)
        m = d["metadata"]
        w = d["wrk2"]
        dr = d.get("derived", {})
        phases = d.get("phases", {})

        records.append({
            "Tool": TOOL_LABELS.get(m["tool"], m["tool"]),
            "scenario": m["scenario"],
            "Scenario": SCENARIO_NAMES.get(m["scenario"], m["scenario"]),
            "Category": CATEGORY_MAP.get(m["scenario"], "Unknown"),
            "run": m["run"],
            "Throughput (rps)": w["throughput_rps"],
            "Latency p50 (ms)": w["latency_ms"]["p50"],
            "Latency p95 (ms)": w["latency_ms"]["p95"],
            "Latency p99 (ms)": w["latency_ms"]["p99"],
            "Mean Latency (ms)": w["latency_ms"]["mean"],
            "Timeouts": w["errors"]["timeout"],
            "HTTP Errors": w["errors"]["http_non2xx3xx"],
            "Read Errors": w["errors"]["read"],
            "Write Errors": w["errors"]["write"],
            "Total Requests": w["requests_total"],
            "Error Rate": (
                (w["errors"]["timeout"] + w["errors"]["http_non2xx3xx"] +
                 w["errors"]["read"] + w["errors"]["write"]) /
                max(w["requests_total"], 1)
            ),
            "Pod Restarts": dr.get("pod_restarts_during_fault", 0),
            "CPU Spike (%)": dr.get("cpu_spike_pct", 0),
            "Memory Spike (MB)": dr.get("memory_spike_mb", 0),
        })

    return pd.DataFrame(records)


def save_fig(fig, name):
    fig.savefig(FIG_DIR / f"{name}.pdf", format="pdf")
    fig.savefig(FIG_DIR / f"{name}.png", format="png")
    print(f"  Saved: {name}.pdf / {name}.png")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Figure 3: Throughput box plots by scenario
# ---------------------------------------------------------------------------

def fig3_throughput_boxplots(df):
    """Side-by-side box plots of throughput for each scenario, grouped by tool."""
    fig, ax = plt.subplots(figsize=(12, 5))

    scenario_labels = [SCENARIO_NAMES[s] for s in SCENARIO_ORDER]
    positions = np.arange(len(SCENARIO_ORDER))
    width = 0.35

    for i, tool in enumerate(["Chaos Mesh", "LitmusChaos"]):
        tool_data = df[df["Tool"] == tool]
        box_data = []
        for sc in SCENARIO_ORDER:
            vals = tool_data[tool_data["scenario"] == sc]["Throughput (rps)"].values
            box_data.append(vals)

        bp = ax.boxplot(
            box_data,
            positions=positions + (i - 0.5) * width,
            widths=width * 0.8,
            patch_artist=True,
            showfliers=True,
            flierprops={"marker": "o", "markersize": 4, "alpha": 0.5},
        )
        color = TOOL_COLORS[tool]
        for patch in bp["boxes"]:
            patch.set_facecolor(color)
            patch.set_alpha(0.7)
        for element in ["whiskers", "caps", "medians"]:
            for line in bp[element]:
                line.set_color("black")
                line.set_linewidth(0.8)
        bp["medians"][0].set_label(tool)  # only first for legend

    ax.set_xticks(positions)
    ax.set_xticklabels(scenario_labels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Throughput (requests/second)")
    ax.set_title("Throughput Comparison: Chaos Mesh vs LitmusChaos")
    ax.legend(loc="upper right")

    # Add category separators
    category_bounds = [0, 3, 8, 10]  # Pod, Network, Resource, Application boundaries
    for b in category_bounds:
        ax.axvline(x=b - 0.5, color="gray", linewidth=0.5, linestyle="--", alpha=0.5)

    # Category labels at bottom
    cat_positions = [1, 5.5, 8.5, 10.5]
    cat_names = ["Pod/Container", "Network", "Resource", "App"]
    for pos, name in zip(cat_positions, cat_names):
        ax.text(pos, ax.get_ylim()[0] - 5, name, ha="center", fontsize=7,
                fontstyle="italic", color="gray")

    fig.tight_layout()
    save_fig(fig, "fig3_throughput_boxplots")


# ---------------------------------------------------------------------------
# Figure 4: Latency p99 comparison
# ---------------------------------------------------------------------------

def fig4_latency_comparison(df):
    """Grouped bar chart of p99 latency per scenario."""
    fig, ax = plt.subplots(figsize=(12, 5))

    scenario_labels = [SCENARIO_NAMES[s] for s in SCENARIO_ORDER]
    x = np.arange(len(SCENARIO_ORDER))
    width = 0.35

    for i, tool in enumerate(["Chaos Mesh", "LitmusChaos"]):
        tool_data = df[df["Tool"] == tool]
        means = []
        stds = []
        for sc in SCENARIO_ORDER:
            vals = tool_data[tool_data["scenario"] == sc]["Latency p99 (ms)"].values
            means.append(np.mean(vals))
            stds.append(np.std(vals, ddof=1))

        bars = ax.bar(
            x + (i - 0.5) * width,
            means,
            width * 0.85,
            yerr=stds,
            label=tool,
            color=TOOL_COLORS[tool],
            alpha=0.8,
            capsize=3,
            error_kw={"linewidth": 0.8},
        )

    ax.set_xticks(x)
    ax.set_xticklabels(scenario_labels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("p99 Latency (ms)")
    ax.set_title("p99 Latency Comparison by Fault Scenario")
    ax.legend(loc="upper right")
    ax.set_ylim(0, 500)

    fig.tight_layout()
    save_fig(fig, "fig4_latency_p99")


# ---------------------------------------------------------------------------
# Figure 5: Error rate comparison
# ---------------------------------------------------------------------------

def fig5_error_rates(df):
    """Grouped bar chart of error rates per scenario."""
    fig, ax = plt.subplots(figsize=(12, 5))

    scenario_labels = [SCENARIO_NAMES[s] for s in SCENARIO_ORDER]
    x = np.arange(len(SCENARIO_ORDER))
    width = 0.35

    for i, tool in enumerate(["Chaos Mesh", "LitmusChaos"]):
        tool_data = df[df["Tool"] == tool]
        means = []
        stds = []
        for sc in SCENARIO_ORDER:
            vals = tool_data[tool_data["scenario"] == sc]["Error Rate"].values
            means.append(np.mean(vals) * 100)  # Convert to percentage
            stds.append(np.std(vals, ddof=1) * 100)

        ax.bar(
            x + (i - 0.5) * width,
            means,
            width * 0.85,
            yerr=stds,
            label=tool,
            color=TOOL_COLORS[tool],
            alpha=0.8,
            capsize=3,
            error_kw={"linewidth": 0.8},
        )

    ax.set_xticks(x)
    ax.set_xticklabels(scenario_labels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Error Rate (%)")
    ax.set_title("Error Rate Comparison by Fault Scenario")
    ax.legend(loc="upper right")

    fig.tight_layout()
    save_fig(fig, "fig5_error_rates")


# ---------------------------------------------------------------------------
# Figure 6: CPU and Memory overhead heatmap
# ---------------------------------------------------------------------------

def fig6_overhead_heatmap(df):
    """Heatmap showing CPU spike and memory spike by scenario and tool."""
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    for ax_idx, (metric, title, fmt) in enumerate([
        ("CPU Spike (%)", "CPU Spike During Fault (%)", ".0f"),
        ("Memory Spike (MB)", "Memory Spike During Fault (MB)", ".1f"),
    ]):
        ax = axes[ax_idx]
        pivot_data = []
        for tool in ["Chaos Mesh", "LitmusChaos"]:
            tool_row = []
            for sc in SCENARIO_ORDER:
                vals = df[(df["Tool"] == tool) & (df["scenario"] == sc)][metric].values
                tool_row.append(np.mean(vals))
            pivot_data.append(tool_row)

        pivot_df = pd.DataFrame(
            pivot_data,
            index=["Chaos Mesh", "LitmusChaos"],
            columns=[SCENARIO_NAMES[s] for s in SCENARIO_ORDER],
        )

        sns.heatmap(
            pivot_df,
            ax=ax,
            annot=True,
            fmt=fmt,
            cmap="YlOrRd",
            linewidths=0.5,
            cbar_kws={"shrink": 0.8},
        )
        ax.set_title(title, fontsize=10)
        ax.set_xticklabels(ax.get_xticklabels(), rotation=45, ha="right", fontsize=7)

    fig.tight_layout()
    save_fig(fig, "fig6_overhead_heatmap")


# ---------------------------------------------------------------------------
# Figure 7: Throughput by fault category (grouped box plots)
# ---------------------------------------------------------------------------

def fig7_category_boxplots(df):
    """Box plots of throughput grouped by fault category and tool."""
    fig, ax = plt.subplots(figsize=(8, 5))

    cat_order = CATEGORY_ORDER
    positions = np.arange(len(cat_order))
    width = 0.35

    for i, tool in enumerate(["Chaos Mesh", "LitmusChaos"]):
        tool_data = df[df["Tool"] == tool]
        box_data = []
        for cat in cat_order:
            vals = tool_data[tool_data["Category"] == cat]["Throughput (rps)"].values
            box_data.append(vals)

        bp = ax.boxplot(
            box_data,
            positions=positions + (i - 0.5) * width,
            widths=width * 0.8,
            patch_artist=True,
            showfliers=True,
            flierprops={"marker": "o", "markersize": 4, "alpha": 0.5},
        )
        color = TOOL_COLORS[tool]
        for patch in bp["boxes"]:
            patch.set_facecolor(color)
            patch.set_alpha(0.7)
        for element in ["whiskers", "caps", "medians"]:
            for line in bp[element]:
                line.set_color("black")
                line.set_linewidth(0.8)

    # Create manual legend
    from matplotlib.patches import Patch
    legend_elements = [
        Patch(facecolor=TOOL_COLORS["Chaos Mesh"], alpha=0.7, label="Chaos Mesh"),
        Patch(facecolor=TOOL_COLORS["LitmusChaos"], alpha=0.7, label="LitmusChaos"),
    ]
    ax.legend(handles=legend_elements, loc="upper right")

    ax.set_xticks(positions)
    ax.set_xticklabels(cat_order, fontsize=10)
    ax.set_ylabel("Throughput (requests/second)")
    ax.set_title("Throughput by Fault Category")

    fig.tight_layout()
    save_fig(fig, "fig7_category_boxplots")


# ---------------------------------------------------------------------------
# Figure 8: Pod restarts comparison
# ---------------------------------------------------------------------------

def fig8_pod_restarts(df):
    """Stacked bar chart showing pod restarts by scenario."""
    fig, ax = plt.subplots(figsize=(10, 4))

    scenario_labels = [SCENARIO_NAMES[s] for s in SCENARIO_ORDER]
    x = np.arange(len(SCENARIO_ORDER))
    width = 0.35

    for i, tool in enumerate(["Chaos Mesh", "LitmusChaos"]):
        tool_data = df[df["Tool"] == tool]
        means = []
        for sc in SCENARIO_ORDER:
            vals = tool_data[tool_data["scenario"] == sc]["Pod Restarts"].values
            means.append(np.mean(vals))

        ax.bar(
            x + (i - 0.5) * width,
            means,
            width * 0.85,
            label=tool,
            color=TOOL_COLORS[tool],
            alpha=0.8,
        )

    ax.set_xticks(x)
    ax.set_xticklabels(scenario_labels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Mean Pod Restarts During Fault")
    ax.set_title("Pod Restarts by Fault Scenario")
    ax.legend(loc="upper right")
    ax.yaxis.set_major_locator(mticker.MaxNLocator(integer=True))

    fig.tight_layout()
    save_fig(fig, "fig8_pod_restarts")


# ---------------------------------------------------------------------------
# Figure 9: Throughput variability (coefficient of variation)
# ---------------------------------------------------------------------------

def fig9_variability(df):
    """Bar chart showing coefficient of variation for throughput."""
    fig, ax = plt.subplots(figsize=(12, 4))

    scenario_labels = [SCENARIO_NAMES[s] for s in SCENARIO_ORDER]
    x = np.arange(len(SCENARIO_ORDER))
    width = 0.35

    for i, tool in enumerate(["Chaos Mesh", "LitmusChaos"]):
        tool_data = df[df["Tool"] == tool]
        cvs = []
        for sc in SCENARIO_ORDER:
            vals = tool_data[tool_data["scenario"] == sc]["Throughput (rps)"].values
            m = np.mean(vals)
            s = np.std(vals, ddof=1)
            cv = (s / m * 100) if m > 0 else 0
            cvs.append(cv)

        ax.bar(
            x + (i - 0.5) * width,
            cvs,
            width * 0.85,
            label=tool,
            color=TOOL_COLORS[tool],
            alpha=0.8,
        )

    ax.set_xticks(x)
    ax.set_xticklabels(scenario_labels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Coefficient of Variation (%)")
    ax.set_title("Throughput Variability Across Repetitions")
    ax.legend(loc="upper right")

    fig.tight_layout()
    save_fig(fig, "fig9_variability")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Loading data...")
    df = load_data()
    print(f"  {len(df)} records loaded")

    print("\nGenerating figures...")
    fig3_throughput_boxplots(df)
    fig4_latency_comparison(df)
    fig5_error_rates(df)
    fig6_overhead_heatmap(df)
    fig7_category_boxplots(df)
    fig8_pod_restarts(df)
    fig9_variability(df)

    print(f"\nAll figures saved to {FIG_DIR}/")


if __name__ == "__main__":
    main()
