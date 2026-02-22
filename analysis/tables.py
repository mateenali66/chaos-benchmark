#!/usr/bin/env python3
"""
Generate LaTeX tables for Paper 4.

Outputs LaTeX table fragments to analysis/tables/ for direct inclusion.
Also outputs CSV versions for review.

Tables:
    Table 2: Tool feature comparison
    Table 3: Fault injection scenarios and parameters
    Table 4: Per-scenario results (throughput, latency, error rate)
    Table 5: Statistical test results
    Table 6: Overhead comparison
"""

import json
import glob
import csv
import math
from collections import defaultdict
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
TABLE_DIR = Path(__file__).parent / "tables"
TABLE_DIR.mkdir(exist_ok=True)

SCENARIO_NAMES = {
    "p1": "Pod Kill", "p2": "Container Kill", "p3": "Pod Failure",
    "n1": "Latency 50ms", "n2": "Latency 100ms", "n3": "Latency 300ms",
    "n4": "Packet Loss 5\\%", "n5": "Network Partition",
    "r1": "CPU Stress 80\\%", "r2": "Memory Pressure 80\\%",
    "a1": "HTTP Abort 503", "a2": "gRPC Unavailable",
}
SCENARIO_ORDER = ["p1", "p2", "p3", "n1", "n2", "n3", "n4", "n5", "r1", "r2", "a1", "a2"]
CATEGORIES = {
    "p1": "Pod/Container", "p2": "Pod/Container", "p3": "Pod/Container",
    "n1": "Network", "n2": "Network", "n3": "Network", "n4": "Network", "n5": "Network",
    "r1": "Resource", "r2": "Resource", "a1": "Application", "a2": "Application",
}
TOOL_LABELS = {"chaos-mesh": "Chaos Mesh", "litmus": "LitmusChaos"}


def mean(xs): return sum(xs) / len(xs) if xs else 0.0
def stdev(xs):
    if len(xs) < 2: return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
def ci95(xs):
    if len(xs) < 2: return 0.0
    return 2.776 * stdev(xs) / math.sqrt(len(xs))


def load_data():
    records = []
    for fpath in sorted(glob.glob(str(DATA_DIR / "*" / "*" / "run-*.json"))):
        with open(fpath) as f:
            d = json.load(f)
        m = d["metadata"]
        w = d["wrk2"]
        dr = d.get("derived", {})
        records.append({
            "tool": m["tool"], "scenario": m["scenario"], "run": m["run"],
            "throughput_rps": w["throughput_rps"],
            "latency_p50": w["latency_ms"]["p50"],
            "latency_p99": w["latency_ms"]["p99"],
            "latency_mean": w["latency_ms"]["mean"],
            "errors_timeout": w["errors"]["timeout"],
            "errors_http": w["errors"]["http_non2xx3xx"],
            "errors_read": w["errors"]["read"],
            "errors_write": w["errors"]["write"],
            "requests_total": w["requests_total"],
            "error_rate": (w["errors"]["timeout"] + w["errors"]["http_non2xx3xx"] +
                           w["errors"]["read"] + w["errors"]["write"]) / max(w["requests_total"], 1),
            "pod_restarts": dr.get("pod_restarts_during_fault", 0),
            "cpu_spike_pct": dr.get("cpu_spike_pct", 0),
            "memory_spike_mb": dr.get("memory_spike_mb", 0),
        })
    return records


# ---------------------------------------------------------------------------
# Table 2: Tool Feature Comparison (static)
# ---------------------------------------------------------------------------
def table2_tool_comparison():
    latex = r"""\begin{table}[ht]
\centering
\caption{Chaos engineering tool comparison}
\label{tab:tool-comparison}
\begin{tabular}{lcc}
\hline
\textbf{Feature} & \textbf{Chaos Mesh} & \textbf{LitmusChaos} \\
\hline
Version & 2.8.1 & 3.26.0 \\
License & Apache 2.0 & Apache 2.0 \\
CNCF Status & Incubating & Incubating \\
Installation & Helm chart & Helm + operator \\
Fault definition & CRDs (YAML) & CRDs (YAML) \\
Pod faults & \checkmark & \checkmark \\
Network faults & \checkmark & \checkmark \\
Resource faults & \checkmark & \checkmark \\
HTTP faults & \checkmark (HTTPChaos) & \checkmark (pod-http-status-code) \\
Dashboard & Web UI & ChaosCenter UI \\
Observability & Grafana plugin & Resilience probes \\
AI/ML features & None & Resilience Score \\
GitHub stars & 6.9k+ & 4.4k+ \\
\hline
\end{tabular}
\end{table}"""
    with open(TABLE_DIR / "table2_tool_comparison.tex", "w") as f:
        f.write(latex)
    print("  Written: table2_tool_comparison.tex")


# ---------------------------------------------------------------------------
# Table 3: Fault Injection Scenarios (static)
# ---------------------------------------------------------------------------
def table3_scenarios():
    latex = r"""\begin{table}[ht]
\centering
\caption{Fault injection scenarios and parameters}
\label{tab:scenarios}
\begin{tabular}{clllc}
\hline
\textbf{ID} & \textbf{Category} & \textbf{Scenario} & \textbf{Parameters} & \textbf{Target} \\
\hline
P1 & Pod/Container & Pod Kill & mode: one & compose-post \\
P2 & Pod/Container & Container Kill & mode: one & compose-post \\
P3 & Pod/Container & Pod Failure & duration: 120s & compose-post \\
N1 & Network & Latency Injection & 50ms delay & compose-post \\
N2 & Network & Latency Injection & 100ms delay & compose-post \\
N3 & Network & Latency Injection & 300ms delay & compose-post \\
N4 & Network & Packet Loss & 5\% loss rate & compose-post \\
N5 & Network & Network Partition & full partition & compose-post \\
R1 & Resource & CPU Stress & 80\% utilization & compose-post \\
R2 & Resource & Memory Pressure & 80\% utilization & compose-post \\
A1 & Application & HTTP Abort & 503 status code & nginx-thrift \\
A2 & Application & gRPC Error & UNAVAILABLE & user-service \\
\hline
\end{tabular}
\end{table}"""
    with open(TABLE_DIR / "table3_scenarios.tex", "w") as f:
        f.write(latex)
    print("  Written: table3_scenarios.tex")


# ---------------------------------------------------------------------------
# Table 4: Per-Scenario Results
# ---------------------------------------------------------------------------
def table4_results(records):
    groups = defaultdict(list)
    for r in records:
        groups[(r["tool"], r["scenario"])].append(r)

    lines = [
        r"\begin{table*}[ht]",
        r"\centering",
        r"\caption{Per-scenario benchmark results (mean $\pm$ 95\% CI, $n=5$ repetitions)}",
        r"\label{tab:results}",
        r"\small",
        r"\begin{tabular}{cl" + "rr" * 3 + "}",
        r"\hline",
        r"\textbf{ID} & \textbf{Scenario} & \multicolumn{2}{c}{\textbf{Throughput (rps)}} & \multicolumn{2}{c}{\textbf{p99 Latency (ms)}} & \multicolumn{2}{c}{\textbf{Error Rate}} \\",
        r"& & CM & LT & CM & LT & CM & LT \\",
        r"\hline",
    ]

    prev_cat = None
    for sc in SCENARIO_ORDER:
        cat = CATEGORIES[sc]
        if cat != prev_cat:
            if prev_cat is not None:
                lines.append(r"\hline")
            prev_cat = cat

        cm = groups.get(("chaos-mesh", sc), [])
        lt = groups.get(("litmus", sc), [])

        cm_rps = [r["throughput_rps"] for r in cm]
        lt_rps = [r["throughput_rps"] for r in lt]
        cm_p99 = [r["latency_p99"] for r in cm]
        lt_p99 = [r["latency_p99"] for r in lt]
        cm_err = [r["error_rate"] for r in cm]
        lt_err = [r["error_rate"] for r in lt]

        def fmt_ci(vals, dec=1):
            m = mean(vals)
            c = ci95(vals)
            if dec == 1:
                return f"${m:.1f} \\pm {c:.1f}$"
            return f"${m:.2f} \\pm {c:.2f}$"

        lines.append(
            f"{sc.upper()} & {SCENARIO_NAMES[sc]} & "
            f"{fmt_ci(cm_rps)} & {fmt_ci(lt_rps)} & "
            f"{fmt_ci(cm_p99)} & {fmt_ci(lt_p99)} & "
            f"{fmt_ci(cm_err, 2)} & {fmt_ci(lt_err, 2)} \\\\"
        )

    # Overall row
    all_cm = [r for r in records if r["tool"] == "chaos-mesh"]
    all_lt = [r for r in records if r["tool"] == "litmus"]
    cm_rps_all = [r["throughput_rps"] for r in all_cm]
    lt_rps_all = [r["throughput_rps"] for r in all_lt]
    cm_p99_all = [r["latency_p99"] for r in all_cm]
    lt_p99_all = [r["latency_p99"] for r in all_lt]
    cm_err_all = [r["error_rate"] for r in all_cm]
    lt_err_all = [r["error_rate"] for r in all_lt]

    lines.append(r"\hline")
    lines.append(
        f"& \\textbf{{Overall}} & "
        f"$\\mathbf{{{mean(cm_rps_all):.1f} \\pm {ci95(cm_rps_all):.1f}}}$ & "
        f"$\\mathbf{{{mean(lt_rps_all):.1f} \\pm {ci95(lt_rps_all):.1f}}}$ & "
        f"$\\mathbf{{{mean(cm_p99_all):.1f} \\pm {ci95(cm_p99_all):.1f}}}$ & "
        f"$\\mathbf{{{mean(lt_p99_all):.1f} \\pm {ci95(lt_p99_all):.1f}}}$ & "
        f"$\\mathbf{{{mean(cm_err_all):.2f} \\pm {ci95(cm_err_all):.2f}}}$ & "
        f"$\\mathbf{{{mean(lt_err_all):.2f} \\pm {ci95(lt_err_all):.2f}}}$ \\\\"
    )

    lines += [
        r"\hline",
        r"\end{tabular}",
        r"\vspace{2mm}",
        r"\raggedright\footnotesize CM = Chaos Mesh, LT = LitmusChaos. Values shown as mean $\pm$ 95\% CI ($t$-distribution, $df=4$).",
        r"\end{table*}",
    ]

    with open(TABLE_DIR / "table4_results.tex", "w") as f:
        f.write("\n".join(lines))
    print("  Written: table4_results.tex")


# ---------------------------------------------------------------------------
# Table 5: Statistical Test Results
# ---------------------------------------------------------------------------
def table5_statistical_tests(records):
    """Wilcoxon + Cliff's delta for throughput per scenario."""
    from analyze import wilcoxon_signed_rank, cliffs_delta, bonferroni

    groups = defaultdict(list)
    for r in records:
        groups[(r["tool"], r["scenario"])].append(r)

    lines = [
        r"\begin{table}[ht]",
        r"\centering",
        r"\caption{Statistical comparison of throughput: Chaos Mesh vs LitmusChaos}",
        r"\label{tab:stats}",
        r"\small",
        r"\begin{tabular}{clrrrrrl}",
        r"\hline",
        r"\textbf{ID} & \textbf{Scenario} & \textbf{CM} & \textbf{LT} & \textbf{$\Delta$\%} & \textbf{$W$} & \textbf{$p$} & \textbf{Cliff's $d$} \\",
        r"\hline",
    ]

    p_values = []
    row_data = []

    for sc in SCENARIO_ORDER:
        cm = sorted(groups.get(("chaos-mesh", sc), []), key=lambda r: r["run"])
        lt = sorted(groups.get(("litmus", sc), []), key=lambda r: r["run"])
        cm_rps = [r["throughput_rps"] for r in cm]
        lt_rps = [r["throughput_rps"] for r in lt]

        W, p = wilcoxon_signed_rank(cm_rps, lt_rps)
        d, mag = cliffs_delta(cm_rps, lt_rps)
        diff_pct = (mean(cm_rps) - mean(lt_rps)) / mean(lt_rps) * 100

        p_values.append(p)
        row_data.append((sc, mean(cm_rps), mean(lt_rps), diff_pct, W, p, d, mag))

    adjusted, significant = bonferroni(p_values)

    for i, (sc, cm_m, lt_m, diff, W, p, d, mag) in enumerate(row_data):
        sig_marker = "$^*$" if significant[i] else ""
        p_str = f"{adjusted[i]:.3f}" if adjusted[i] >= 0.001 else "$<$0.001"
        lines.append(
            f"{sc.upper()} & {SCENARIO_NAMES[sc]} & {cm_m:.1f} & {lt_m:.1f} & "
            f"{diff:+.1f} & {W:.0f} & {p_str}{sig_marker} & "
            f"{d:+.3f} ({mag}) \\\\"
        )

    lines += [
        r"\hline",
        r"\end{tabular}",
        r"\vspace{2mm}",
        r"\raggedright\footnotesize Wilcoxon signed-rank test ($n=5$ paired observations). $p$-values adjusted with Bonferroni correction ($m=12$). $^*$Significant at $\alpha=0.05$ after correction. Cliff's $d$: $|d|<0.147$ negligible, $<0.33$ small, $<0.474$ medium, $\geq0.474$ large.",
        r"\end{table}",
    ]

    with open(TABLE_DIR / "table5_statistical_tests.tex", "w") as f:
        f.write("\n".join(lines))
    print("  Written: table5_statistical_tests.tex")


# ---------------------------------------------------------------------------
# Table 6: Overhead Comparison
# ---------------------------------------------------------------------------
def table6_overhead(records):
    groups = defaultdict(list)
    for r in records:
        groups[(r["tool"], r["scenario"])].append(r)

    lines = [
        r"\begin{table}[ht]",
        r"\centering",
        r"\caption{Resource overhead during fault injection}",
        r"\label{tab:overhead}",
        r"\small",
        r"\begin{tabular}{clrrrr}",
        r"\hline",
        r"\textbf{ID} & \textbf{Scenario} & \multicolumn{2}{c}{\textbf{CPU Spike (\%)}} & \multicolumn{2}{c}{\textbf{Memory Spike (MB)}} \\",
        r"& & CM & LT & CM & LT \\",
        r"\hline",
    ]

    for sc in SCENARIO_ORDER:
        cm = groups.get(("chaos-mesh", sc), [])
        lt = groups.get(("litmus", sc), [])

        cm_cpu = [r["cpu_spike_pct"] for r in cm]
        lt_cpu = [r["cpu_spike_pct"] for r in lt]
        cm_mem = [r["memory_spike_mb"] for r in cm]
        lt_mem = [r["memory_spike_mb"] for r in lt]

        lines.append(
            f"{sc.upper()} & {SCENARIO_NAMES[sc]} & "
            f"{mean(cm_cpu):.0f} & {mean(lt_cpu):.0f} & "
            f"{mean(cm_mem):.1f} & {mean(lt_mem):.1f} \\\\"
        )

    # Overall
    all_cm = [r for r in records if r["tool"] == "chaos-mesh"]
    all_lt = [r for r in records if r["tool"] == "litmus"]
    lines.append(r"\hline")
    lines.append(
        f"& \\textbf{{Overall}} & "
        f"\\textbf{{{mean([r['cpu_spike_pct'] for r in all_cm]):.0f}}} & "
        f"\\textbf{{{mean([r['cpu_spike_pct'] for r in all_lt]):.0f}}} & "
        f"\\textbf{{{mean([r['memory_spike_mb'] for r in all_cm]):.1f}}} & "
        f"\\textbf{{{mean([r['memory_spike_mb'] for r in all_lt]):.1f}}} \\\\"
    )

    lines += [
        r"\hline",
        r"\end{tabular}",
        r"\vspace{2mm}",
        r"\raggedright\footnotesize CPU Spike = percentage increase in aggregate CPU usage during fault phase relative to baseline. Memory Spike = absolute increase in aggregate memory usage (MB). Negative CPU values indicate the killed pod's CPU contribution dropped to zero.",
        r"\end{table}",
    ]

    with open(TABLE_DIR / "table6_overhead.tex", "w") as f:
        f.write("\n".join(lines))
    print("  Written: table6_overhead.tex")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("Loading data...")
    records = load_data()
    print(f"  {len(records)} records")

    print("\nGenerating tables...")
    table2_tool_comparison()
    table3_scenarios()
    table4_results(records)
    table5_statistical_tests(records)
    table6_overhead(records)

    print(f"\nAll tables saved to {TABLE_DIR}/")


if __name__ == "__main__":
    main()
