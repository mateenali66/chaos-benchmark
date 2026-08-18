#!/usr/bin/env python3
"""
Generate LaTeX tables for Paper 4 from analysis/analyze.py's CSV output.

Reads analysis/results/*.csv only -- does NOT independently reload raw
run JSON or recompute any statistic, so table content cannot drift out of
sync with what analyze.py (the single source of statistical truth) actually
computed. Run analysis/analyze.py first.

Tables:
    Table 2: Tool feature comparison (static)
    Table 3: Fault injection scenarios and parameters (static)
    Table 4: Per-scenario results (median [IQR], n=30 repetitions)
    Table 5: Confirmatory statistical tests (Mann-Whitney U, Holm-Bonferroni,
             Cliff's delta with BCa/percentile-fallback 95% CI)
    Table 6: Resource overhead during fault injection
"""

import csv
from pathlib import Path

RESULTS_DIR = Path(__file__).parent / "results"
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


def _read_csv(name: str) -> list[dict]:
    path = RESULTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"{path} not found -- run analysis/analyze.py first")
    with open(path) as f:
        return list(csv.DictReader(f))


def _index(rows: list[dict], *keys: str) -> dict:
    return {tuple(r[k] for k in keys): r for r in rows}


# ---------------------------------------------------------------------------
# Table 2: Tool Feature Comparison (static -- no experimental data involved)
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
# Table 4: Per-Scenario Results (median [IQR], n=30)
# ---------------------------------------------------------------------------
def table4_results():
    summary = _index(_read_csv("summary_stats.csv"), "tool", "scenario")
    n = next(iter(summary.values()))["n"] if summary else "?"

    lines = [
        r"\begin{table*}[ht]",
        r"\centering",
        rf"\caption{{Per-scenario benchmark results (median [IQR], $n={n}$ repetitions)}}",
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

        cm = summary.get(("Chaos Mesh", sc))
        lt = summary.get(("LitmusChaos", sc))
        if not cm or not lt:
            continue

        def fmt(row, key, dec=1):
            med = float(row[f"{key}_median"])
            if f"{key}_q1" in row and f"{key}_q3" in row:
                q1, q3 = float(row[f"{key}_q1"]), float(row[f"{key}_q3"])
                return f"${med:.{dec}f}$ [{q1:.{dec}f}, {q3:.{dec}f}]"
            return f"${med:.{dec}f}$"

        lines.append(
            f"{sc.upper()} & {SCENARIO_NAMES[sc]} & "
            f"{fmt(cm, 'rps')} & {fmt(lt, 'rps')} & "
            f"{fmt(cm, 'p99')} & {fmt(lt, 'p99')} & "
            f"{fmt(cm, 'error_rate', 3)} & {fmt(lt, 'error_rate', 3)} \\\\"
        )

    lines += [
        r"\hline",
        r"\end{tabular}",
        r"\vspace{2mm}",
        r"\raggedright\footnotesize CM = Chaos Mesh, LT = LitmusChaos. Values shown as median [Q1, Q3] across "
        rf"$n={n}$ repetitions per (tool, scenario) cell (crossover-allocated across two clusters, "
        r"amendment item 1). Throughput and p99 latency are aggregated over the whole "
        r"baseline+fault+recovery protocol window, not fault-phase-isolated (see PREREGISTRATION.md).",
        r"\end{table*}",
    ]

    with open(TABLE_DIR / "table4_results.tex", "w") as f:
        f.write("\n".join(lines))
    print("  Written: table4_results.tex")


# ---------------------------------------------------------------------------
# Table 5: Confirmatory Statistical Tests (primary metric family only)
# ---------------------------------------------------------------------------
def table5_statistical_tests():
    tests = _read_csv("statistical_tests.csv")
    primary = {r["scenario"]: r for r in tests
               if r["metric"] == "Throughput (rps)"}

    lines = [
        r"\begin{table}[ht]",
        r"\centering",
        r"\caption{Confirmatory statistical comparison of throughput: Chaos Mesh vs LitmusChaos}",
        r"\label{tab:stats}",
        r"\small",
        r"\begin{tabular}{clrrrrrl}",
        r"\hline",
        r"\textbf{ID} & \textbf{Scenario} & \textbf{CM} & \textbf{LT} & \textbf{$\Delta$\%} & \textbf{$U$} & \textbf{$p_{\mathrm{Holm}}$} & \textbf{Cliff's $d$} \\",
        r"\hline",
    ]

    for sc in SCENARIO_ORDER:
        row = primary.get(sc)
        if not row:
            continue
        sig = row["significant_after_holm"] in ("True", "true", "1")
        sig_marker = "$^*$" if sig else ""
        p_adj = float(row["p_adjusted_holm"])
        p_str = f"{p_adj:.3f}" if p_adj >= 0.001 else "$<$0.001"
        diff = float(row["diff_pct"]) if row["diff_pct"] not in ("", "None") else float("nan")
        lines.append(
            f"{sc.upper()} & {SCENARIO_NAMES[sc]} & {float(row['cm_median']):.1f} & "
            f"{float(row['lt_median']):.1f} & {diff:+.1f} & {float(row['U_statistic']):.0f} & "
            f"{p_str}{sig_marker} & {float(row['cliffs_delta']):+.3f} ({row['effect_size']}) \\\\"
        )

    n_cm = primary[SCENARIO_ORDER[0]]["n_cm"] if primary else "?"
    lines += [
        r"\hline",
        r"\end{tabular}",
        r"\vspace{2mm}",
        rf"\raggedright\footnotesize Two-sided Mann-Whitney U test (unpaired, $n={n_cm}$ per arm). "
        r"$p$-values are Holm-Bonferroni corrected within the throughput (primary) metric family, $m=12$. "
        r"$^*$Significant at $\alpha=0.05$ after correction. Cliff's $d$: $|d|<0.147$ negligible, "
        r"$<0.33$ small, $<0.474$ medium, $\geq0.474$ large. 95\% CIs for $d$ "
        r"(BCa bootstrap, 10{,}000 resamples; percentile fallback where BCa is degenerate under "
        r"perfect separation) are in \texttt{analysis/results/statistical\_tests.csv}, omitted here for space.",
        r"\end{table}",
    ]

    with open(TABLE_DIR / "table5_statistical_tests.tex", "w") as f:
        f.write("\n".join(lines))
    print("  Written: table5_statistical_tests.tex")


# ---------------------------------------------------------------------------
# Table 6: Overhead / Resource Comparison
# ---------------------------------------------------------------------------
def table6_overhead():
    summary = _index(_read_csv("summary_stats.csv"), "tool", "scenario")

    lines = [
        r"\begin{table}[ht]",
        r"\centering",
        r"\caption{Recovery time and pod restarts during fault injection (median, n=30)}",
        r"\label{tab:overhead}",
        r"\small",
        r"\begin{tabular}{clrrrr}",
        r"\hline",
        r"\textbf{ID} & \textbf{Scenario} & \multicolumn{2}{c}{\textbf{Recovery time (s)}} & \multicolumn{2}{c}{\textbf{Pod restarts}} \\",
        r"& & CM & LT & CM & LT \\",
        r"\hline",
    ]

    for sc in SCENARIO_ORDER:
        cm = summary.get(("Chaos Mesh", sc))
        lt = summary.get(("LitmusChaos", sc))
        if not cm or not lt:
            continue

        def rec_s(row):
            v = row.get("recovery_time_median_s")
            return f"{float(v):.1f}" if v not in (None, "", "None") else "--"

        lines.append(
            f"{sc.upper()} & {SCENARIO_NAMES[sc]} & "
            f"{rec_s(cm)} & {rec_s(lt)} & "
            f"{float(cm['pod_restarts_median']):.0f} & {float(lt['pod_restarts_median']):.0f} \\\\"
        )

    lines += [
        r"\hline",
        r"\end{tabular}",
        r"\vspace{2mm}",
        r"\raggedright\footnotesize Recovery time = elapsed seconds from fault end until mean CPU across "
        r"faulted pods first returns to within 20\% of that run's own baseline-phase CPU mean, "
        r"right-censored at 60s (this study's recovery-phase length) if never reached; an operational "
        r"definition introduced for this analysis, not literally pre-specified beyond naming "
        r"``recovery time'' as a metric (see PREREGISTRATION.md and analysis/analyze.py docstring).",
        r"\end{table}",
    ]

    with open(TABLE_DIR / "table6_overhead.tex", "w") as f:
        f.write("\n".join(lines))
    print("  Written: table6_overhead.tex")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("Generating tables from analysis/results/ CSVs...")
    table2_tool_comparison()
    table3_scenarios()
    table4_results()
    table5_statistical_tests()
    table6_overhead()
    print(f"\nAll tables saved to {TABLE_DIR}/")


if __name__ == "__main__":
    main()
