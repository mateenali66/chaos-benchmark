#!/usr/bin/env python3
"""
Component 1 figures, drawn from analysis/results/runs_long.csv.

Writes each figure to analysis/figures/ as PDF (vector) and PNG (300 DPI).

Figures:
    Fig 3: throughput box plots by scenario and tool
    Fig 4: p99 latency by scenario (median and IQR, log scale)
    Fig 5: error rate by scenario
    Fig 6: CPU and memory spike heatmap
    Fig 7: throughput box plots by fault category
    Fig 8: pod restarts by scenario
    Fig 9: throughput coefficient of variation by scenario
"""

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

RESULTS_DIR = Path(__file__).parent / "results"
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
    """Read analysis/results/runs_long.csv, written by analyze.py. Figures
    never parse raw run JSON, so they stay in sync with the statistics. Run
    analysis/analyze.py first."""
    path = RESULTS_DIR / "runs_long.csv"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found -- run analysis/analyze.py first")
    raw = pd.read_csv(path)
    df = pd.DataFrame({
        "Tool": raw["tool"].map(TOOL_LABELS).fillna(raw["tool"]),
        "scenario": raw["scenario"],
        "Scenario": raw["scenario"].map(SCENARIO_NAMES).fillna(raw["scenario"]),
        "Category": raw["category"],
        "run": raw["run"],
        "cluster": raw["cluster"],
        "Throughput (rps)": raw["throughput_rps"],
        "Latency p99 (ms)": raw["latency_p99"],
        "Mean Latency (ms)": raw["latency_mean"],
        "Error Rate": raw["error_rate"],
        "Pod Restarts": raw["pod_restarts"],
        "CPU Spike (%)": raw["cpu_spike_pct"],
        "Memory Spike (MB)": raw["memory_spike_mb"],
        "Recovery Time (s)": raw["recovery_time_s"],
    })
    return df


def save_fig(fig, name):
    fig.savefig(FIG_DIR / f"{name}.pdf", format="pdf")
    fig.savefig(FIG_DIR / f"{name}.png", format="png")
    print(f"  Saved: {name}.pdf / {name}.png")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Figure 3: Throughput box plots by scenario
# ---------------------------------------------------------------------------

# Print style for fig3 and fig4, which span the full page width (figure*,
# cas-dc \textwidth = 494.5pt = 6.87in). figsize makes the tight-bbox PDF
# ~6.87in wide, so LaTeX scales it by ~1.0 and these point sizes are the
# printed sizes. Applied through rc_context so the other figures keep the
# module-wide style.
FULL_WIDTH_RC = {
    "font.size": 8,
    "axes.labelsize": 9,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "legend.fontsize": 8,
    "axes.linewidth": 0.8,
}
FULL_WIDTH_FIGSIZE = (6.9, 3.4)


def _legend_above(ax, handles, y_offset_pt):
    """Horizontal legend centred above the top spine, outside the axes so it
    cannot cover data. y_offset_pt lifts it clear of anything drawn just
    above the spine (fig3's category labels).
    """
    import matplotlib.transforms as mtransforms
    offset = mtransforms.offset_copy(ax.transAxes, fig=ax.figure, x=0, y=y_offset_pt, units="points")
    ax.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 1.0),
              bbox_transform=offset, ncol=len(handles), frameon=False,
              borderaxespad=0.0, handlelength=1.6, columnspacing=1.5)


def fig3_throughput_boxplots(df):
    """Side-by-side box plots of throughput for each scenario, grouped by tool."""
    with plt.rc_context(FULL_WIDTH_RC):
        _fig3_throughput_boxplots(df)


def _fig3_throughput_boxplots(df):
    fig, ax = plt.subplots(figsize=FULL_WIDTH_FIGSIZE)

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
        # The legend uses color-matched Patches (below), not the median
        # lines, which are black for both tools.

    ax.set_xticks(positions)
    ax.set_xticklabels(scenario_labels, rotation=45, ha="right", rotation_mode="anchor")
    ax.set_ylabel("Throughput (requests/second)")
    ax.set_axisbelow(True)  # grid behind the boxes, not drawn through them
    # No in-figure title. The LaTeX caption carries it.
    from matplotlib.patches import Patch
    legend_elements = [Patch(facecolor=TOOL_COLORS[t], alpha=0.7, label=t) for t in ["Chaos Mesh", "LitmusChaos"]]

    # Add category separators
    category_bounds = [0, 3, 8, 10]  # Pod, Network, Resource, Application boundaries
    for b in category_bounds:
        ax.axvline(x=b - 0.5, color="gray", linewidth=0.5, linestyle="--", alpha=0.5)

    # Category labels sit just above the top spine (x in data coordinates, y
    # in axes fraction). Below the axes they would hit the rotated tick
    # labels, and inside it they would hit the boxes, which reach ~120 rps,
    # the top of the plot, in every category. Network (n1-n5, positions 3-7)
    # is centred at 5.
    cat_positions = [1, 5, 8.5, 10.5]
    cat_names = ["Pod/Container", "Network", "Resource", "Application"]
    for pos, name in zip(cat_positions, cat_names):
        ax.annotate(name, xy=(pos, 1.0), xycoords=ax.get_xaxis_transform(),
                    xytext=(0, 2), textcoords="offset points",
                    ha="center", va="bottom", fontsize=8, fontstyle="italic",
                    color="dimgray", annotation_clip=False)
    _legend_above(ax, legend_elements, y_offset_pt=13)

    fig.tight_layout()
    save_fig(fig, "fig3_throughput_boxplots")


# ---------------------------------------------------------------------------
# Figure 4: Latency p99 comparison
# ---------------------------------------------------------------------------

def fig4_latency_comparison(df):
    """Grouped bar chart of p99 latency per scenario: median bars with IQR
    error bars on a log y-axis.

    p99 latency is heavily right-skewed (some cells' medians reach tens of
    thousands of ms against a steady-state p99 of ~83 ms), so the chart uses
    medians and IQRs, as PREREGISTRATION.md requires for skewed metrics. The
    log scale shows the full range without clipping.
    """
    with plt.rc_context(FULL_WIDTH_RC):
        _fig4_latency_comparison(df)


def _fig4_latency_comparison(df):
    fig, ax = plt.subplots(figsize=FULL_WIDTH_FIGSIZE)

    scenario_labels = [SCENARIO_NAMES[s] for s in SCENARIO_ORDER]
    x = np.arange(len(SCENARIO_ORDER))
    width = 0.35

    for i, tool in enumerate(["Chaos Mesh", "LitmusChaos"]):
        tool_data = df[df["Tool"] == tool]
        medians, err_lo, err_hi = [], [], []
        for sc in SCENARIO_ORDER:
            vals = tool_data[tool_data["scenario"] == sc]["Latency p99 (ms)"].values
            med = np.median(vals)
            q1, q3 = np.percentile(vals, 25), np.percentile(vals, 75)
            medians.append(med)
            err_lo.append(max(med - q1, 0))
            err_hi.append(max(q3 - med, 0))

        ax.bar(
            x + (i - 0.5) * width,
            medians,
            width * 0.85,
            yerr=[err_lo, err_hi],
            label=tool,
            color=TOOL_COLORS[tool],
            alpha=0.8,
            capsize=3,
            error_kw={"linewidth": 0.8},
        )

    ax.set_xticks(x)
    ax.set_xticklabels(scenario_labels, rotation=45, ha="right", rotation_mode="anchor")
    # No in-figure title. "median, IQR" goes in the y label because the
    # LaTeX caption does not say it.
    ax.set_ylabel("p99 latency (ms, log scale)\nbar = median, error bar = IQR")
    ax.set_yscale("log")
    ax.set_axisbelow(True)  # grid behind the bars, not drawn through them
    handles, _ = ax.get_legend_handles_labels()
    _legend_above(ax, handles, y_offset_pt=4)

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

def _compact_thousands(v):
    """11521.3 -> '11.5k', 2202.4 -> '2.2k', 859.2 -> '859'."""
    return f"{v / 1000:.1f}k" if abs(v) >= 1000 else f"{v:.0f}"


def _relative_luminance(rgba):
    """WCAG 2.x relative luminance of an sRGB(A) colour in [0, 1]."""
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgba[:3]]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _annotation_color(rgba):
    """White or black, whichever has the higher WCAG contrast ratio against
    the cell colour. The crossover is at luminance ~0.179, so every cell gets
    at least ~4.6:1. seaborn's own 0.408 cut-off would put white text on the
    mid-orange cells at ~2.3:1."""
    lum = _relative_luminance(rgba)
    contrast_white = 1.05 / (lum + 0.05)
    contrast_black = (lum + 0.05) / 0.05
    return "white" if contrast_white > contrast_black else "black"


# fig6 is drawn for one cas-dc column (\columnwidth = 238.25pt = 3.31in),
# so the point sizes below are the printed sizes.
FIG6_RC = {
    "font.size": 7,
    "axes.titlesize": 8,
    "axes.labelsize": 7,
    "xtick.labelsize": 7,
    "ytick.labelsize": 7,
}


def fig6_overhead_heatmap(df):
    """Heatmap of mean CPU spike and memory spike per scenario and tool.

    Laid out for one column: scenarios are rows and the two tools are
    columns, with the YlOrRd colormap and a separate colour scale per panel.
    CPU values are shown in thousands ("11.5k") so five-digit values fit
    their cells. Text colour is chosen per cell by luminance
    (_annotation_color).
    """
    with plt.rc_context(FIG6_RC):
        _fig6_overhead_heatmap(df)


def _fig6_overhead_heatmap(df):
    fig, axes = plt.subplots(
        2, 2, figsize=(3.15, 2.85), layout="constrained",
        gridspec_kw={"height_ratios": [1, 0.045]},
    )
    fig.get_layout_engine().set(w_pad=0.02, h_pad=0.03, wspace=0.06, hspace=0.02)

    tool_ticklabels = ["Chaos\nMesh", "Litmus\nChaos"]
    for ax_idx, (metric, title, cbar_label, fmt) in enumerate([
        ("CPU Spike (%)", "CPU spike", "Mean spike (%)\nk = thousand", _compact_thousands),
        ("Memory Spike (MB)", "Memory spike", "Mean spike (MB)", lambda v: f"{v:.1f}"),
    ]):
        ax = axes[0, ax_idx]
        cax = axes[1, ax_idx]
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

        # Scenarios as rows, tools as columns (see docstring).
        plot_df = pivot_df.T

        sns.heatmap(
            plot_df,
            ax=ax,
            cbar_ax=cax,
            annot=False,
            cmap="YlOrRd",
            linewidths=0.5,
            cbar_kws={"orientation": "horizontal"},
            yticklabels=(ax_idx == 0),
        )
        # Annotate by hand so the text colour can follow the cell colour.
        mesh = ax.collections[0]
        cmap, norm = mesh.get_cmap(), mesh.norm
        for r, scenario in enumerate(plot_df.index):
            for c, tool in enumerate(plot_df.columns):
                v = plot_df.loc[scenario, tool]
                ax.text(c + 0.5, r + 0.5, fmt(v), ha="center", va="center",
                        fontsize=7, color=_annotation_color(cmap(norm(v))))

        ax.set_title(title, pad=3)
        ax.set_xlabel("")
        ax.set_ylabel("")
        ax.set_xticklabels(tool_ticklabels, rotation=0, linespacing=1.0)
        ax.tick_params(axis="both", length=0, pad=2)
        # Turn off the module-wide grid, which would cross every cell at the
        # tick positions.
        ax.grid(False)
        if ax_idx == 0:
            ax.set_yticklabels(ax.get_yticklabels(), rotation=0)

        cax.set_xlabel(cbar_label, labelpad=2, linespacing=1.1)
        cax.tick_params(length=2, pad=1.5)
        if ax_idx == 0:
            cax.xaxis.set_major_formatter(mticker.FuncFormatter(
                lambda v, _pos: "0" if v == 0 else _compact_thousands(v).replace(".0k", "k")))
            cax.xaxis.set_major_locator(mticker.MaxNLocator(nbins=4))
        else:
            cax.xaxis.set_major_locator(mticker.MaxNLocator(nbins=4, integer=True))

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

    # Manual legend outside the axes. Inside, it would overlap the
    # Application boxes, the rightmost and tallest group.
    from matplotlib.patches import Patch
    legend_elements = [
        Patch(facecolor=TOOL_COLORS["Chaos Mesh"], alpha=0.7, label="Chaos Mesh"),
        Patch(facecolor=TOOL_COLORS["LitmusChaos"], alpha=0.7, label="LitmusChaos"),
    ]
    ax.legend(handles=legend_elements, loc="upper left", bbox_to_anchor=(1.01, 1.0))

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
