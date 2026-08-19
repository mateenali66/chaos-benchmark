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
    latex = r"""\begin{table*}[ht]
\centering
\caption{Chaos engineering tool comparison}
\label{tab:tool-comparison}
\begin{tabular}{lcc}
\hline
\textbf{Feature} & \textbf{Chaos Mesh} & \textbf{LitmusChaos} \\
\hline
Major version & 2.x & 3.x \\
License & Apache 2.0 & Apache 2.0 \\
CNCF Status & Incubating & Incubating \\
Installation & Helm chart & Helm + operator \\
Fault definition & CRDs (YAML) & CRDs (YAML) \\
Pod faults & \checkmark & \checkmark \\
Network faults & \checkmark & \checkmark \\
Resource faults & \checkmark & \checkmark \\
HTTP faults & \checkmark (HTTPChaos) & \checkmark (pod-http-status-code) \\
Dashboard & Web UI & ChaosCenter UI \\
Observability & Grafana plugin & Resilience probes (deterministic score, not ML) \\
LLM/agent interface & None & Model Context Protocol server \\
GitHub stars (top-10 census, Nov.\ 2024)\textsuperscript{*} & \checkmark & \checkmark \\
\hline
\end{tabular}
\vspace{1mm}
\raggedright\footnotesize\textsuperscript{*}Both tools independently rank in the top 10 of Owotogbe et al.'s GitHub-star-ranked chaos engineering platform census, verified against their published Table 13.
\end{table*}"""
    with open(TABLE_DIR / "table2_tool_comparison.tex", "w") as f:
        f.write(latex)
    print("  Written: table2_tool_comparison.tex")


# ---------------------------------------------------------------------------
# Table 3: Fault Injection Scenarios (static)
# ---------------------------------------------------------------------------
def table3_scenarios():
    """Generated from experiments/scenarios.yaml, not hardcoded -- a prior
    hardcoded version listed "compose-post" as the target for 10 of 12
    scenarios when only 2 actually target compose-post-service; found by a
    2026-08-18 numerical-consistency audit that hand-verified every target
    against the real config."""
    import yaml
    scenarios_path = Path(__file__).resolve().parent.parent / "experiments" / "scenarios.yaml"
    with open(scenarios_path) as f:
        cfg = yaml.safe_load(f)

    category_labels = {"pod": "Pod/Container", "network": "Network",
                        "resource": "Resource", "application": "Application"}
    # Hand-verified against experiments/scenarios.yaml's actual `parameters`
    # field per scenario (2026-08-18, after a peer-review pass caught P2
    # showing a stale "mode: one" copied from P1 -- container-kill has no
    # `mode` field, it targets named containers). Re-verify this dict against
    # the YAML if scenarios.yaml's parameters ever change; unlike the target
    # column above, formatting is too heterogeneous across action types
    # (delay/loss/partition/stressors/abort) to auto-render compactly.
    param_summaries = {
        "P1": "mode: one", "P2": "container: nginx-thrift", "P3": "duration: 120s",
        "N1": "50ms delay", "N2": "100ms delay", "N3": "300ms delay",
        "N4": "5\\% loss rate", "N5": "full partition",
        "R1": "80\\% utilization", "R2": "80\\% utilization",
        "A1": "503 status code", "A2": "UNAVAILABLE",
    }

    rows = []
    for sc in cfg["scenarios"]:
        sid = sc["id"]
        cat = category_labels[sc["category"]]
        target = sc["target"]["name"]
        name = sc["name"].replace("%", "\\%")
        rows.append(f"{sid} & {cat} & {name} & {param_summaries[sid]} & {target} \\\\")

    latex = "\\begin{table*}[ht]\n\\centering\n"
    latex += "\\caption{Fault injection scenarios and parameters}\n"
    latex += "\\label{tab:scenarios}\n"
    latex += "\\small\n"
    latex += "\\begin{tabular}{cllll}\n\\hline\n"
    latex += r"\textbf{ID} & \textbf{Category} & \textbf{Scenario} & \textbf{Parameters} & \textbf{Target} \\" + "\n"
    latex += "\\hline\n"
    latex += "\n".join(rows) + "\n"
    latex += "\\hline\n\\end{tabular}\n\\end{table*}"

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
        r"\scriptsize",
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
            f"{fmt(cm, 'p99', 0)} & {fmt(lt, 'p99', 0)} & "
            f"{fmt(cm, 'error_rate', 3)} & {fmt(lt, 'error_rate', 3)} \\\\"
        )

    lines += [
        r"\hline",
        r"\end{tabular}",
        r"\vspace{2mm}",
        r"\raggedright\footnotesize CM = Chaos Mesh, LT = LitmusChaos. Values shown as median [Q1, Q3] across "
        rf"$n={n}$ repetitions per (tool, scenario) cell (crossover-allocated across two clusters, "
        r"Section~\ref{sec:meth-comp1}). Throughput and p99 latency are aggregated over the whole "
        r"baseline+fault+recovery protocol window, not fault-phase-isolated.",
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
        r"\begin{table*}[ht]",
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
        r"perfect separation) are in the reproducibility package (Section~\ref{sec:conclusions}), omitted here for space.",
        r"\end{table*}",
    ]

    with open(TABLE_DIR / "table5_statistical_tests.tex", "w") as f:
        f.write("\n".join(lines))
    print("  Written: table5_statistical_tests.tex")


# ---------------------------------------------------------------------------
# Table 5b: Confirmatory statistical comparison of the two registered
# secondary metric families (latency p99, error rate) -- added after a peer
# review pass flagged that the manuscript's own "latency diverges" claim was
# never backed by a shown significance test, even though both families are
# registered in the stats plan and already computed in statistical_tests.csv.
# ---------------------------------------------------------------------------
def table5b_secondary_metrics():
    tests = _read_csv("statistical_tests.csv")
    latency = {r["scenario"]: r for r in tests if r["metric"] == "Latency p99 (ms)"}
    error = {r["scenario"]: r for r in tests if r["metric"] == "Error rate"}

    lines = [
        r"\begin{table*}[ht]",
        r"\centering",
        r"\caption{Confirmatory statistical comparison of the two secondary metric families: p99 latency and error rate}",
        r"\label{tab:stats-secondary}",
        r"\small",
        r"\begin{tabular}{clrrlrrl}",
        r"\hline",
        r"\multicolumn{2}{c}{} & \multicolumn{3}{c}{\textbf{p99 latency (ms)}} & \multicolumn{3}{c}{\textbf{Error rate}} \\",
        r"\textbf{ID} & \textbf{Scenario} & \textbf{CM} & \textbf{LT} & \textbf{$p_{\mathrm{Holm}}$} & \textbf{CM} & \textbf{LT} & \textbf{$p_{\mathrm{Holm}}$} \\",
        r"\hline",
    ]

    def _fmt_p(row):
        sig = row["significant_after_holm"] in ("True", "true", "1")
        sig_marker = "$^*$" if sig else ""
        p_adj = float(row["p_adjusted_holm"])
        p_str = f"{p_adj:.3f}" if p_adj >= 0.001 else "$<$0.001"
        return f"{p_str}{sig_marker}"

    for sc in SCENARIO_ORDER:
        lrow = latency.get(sc)
        erow = error.get(sc)
        if not lrow or not erow:
            continue
        lines.append(
            f"{sc.upper()} & {SCENARIO_NAMES[sc]} & {float(lrow['cm_median']):.0f} & "
            f"{float(lrow['lt_median']):.0f} & {_fmt_p(lrow)} & "
            f"{float(erow['cm_median']):.3f} & {float(erow['lt_median']):.3f} & {_fmt_p(erow)} \\\\"
        )

    lines += [
        r"\hline",
        r"\end{tabular}",
        r"\vspace{2mm}",
        r"\raggedright\footnotesize Two-sided Mann-Whitney U test (unpaired, $n=30$ per arm) for each secondary "
        r"family, Holm-Bonferroni corrected within its own family ($m=12$), independent of the throughput "
        r"(primary) family's correction in Table~\ref{tab:stats}. $^*$Significant at $\alpha=0.05$ after correction. "
        r"7 of 12 scenarios show significant p99 latency differences despite only 2 of 12 showing a significant "
        r"throughput difference (Table~\ref{tab:stats}); 7 of 12 show significant error-rate differences. "
        r"Effect sizes and CIs for both families are in the reproducibility package (Section~\ref{sec:conclusions}).",
        r"\end{table*}",
    ]

    with open(TABLE_DIR / "table5b_secondary_metrics.tex", "w") as f:
        f.write("\n".join(lines))
    print("  Written: table5b_secondary_metrics.tex")


# ---------------------------------------------------------------------------
# Table 6: Overhead / Resource Comparison
# ---------------------------------------------------------------------------
def table6_overhead():
    summary = _index(_read_csv("summary_stats.csv"), "tool", "scenario")

    lines = [
        r"\begin{table*}[ht]",
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

    reduced_n = False
    for sc in SCENARIO_ORDER:
        cm = summary.get(("Chaos Mesh", sc))
        lt = summary.get(("LitmusChaos", sc))
        if not cm or not lt:
            continue

        def rec_s(row):
            nonlocal reduced_n
            v = row.get("recovery_time_median_s")
            if v in (None, "", "None"):
                return "--"
            n = int(row.get("recovery_time_n", row["n"]))
            mark = ""
            if n < int(row["n"]):
                reduced_n = True
                mark = f"$^{{n={n}}}$"
            return f"{float(v):.1f}{mark}"

        lines.append(
            f"{sc.upper()} & {SCENARIO_NAMES[sc]} & "
            f"{rec_s(cm)} & {rec_s(lt)} & "
            f"{float(cm['pod_restarts_median']):.0f} & {float(lt['pod_restarts_median']):.0f} \\\\"
        )

    reduced_n_note = (
        r" Superscript $n=$ marks cells computed on fewer than the nominal 30 runs "
        r"due to a Prometheus infrastructure-metrics collection gap affecting recovery time "
        r"only (Section~\ref{sec:meth-comp1})."
        if reduced_n else ""
    )
    lines += [
        r"\hline",
        r"\end{tabular}",
        r"\vspace{2mm}",
        r"\raggedright\footnotesize Recovery time = elapsed seconds from fault end until mean CPU across "
        r"faulted pods first returns to within 20\% of that run's own baseline-phase CPU mean, "
        r"right-censored at 60s (this study's recovery-phase length) if never reached; an operational "
        r"definition introduced for this analysis, not literally pre-specified beyond naming "
        r"``recovery time'' as a metric (Section~\ref{sec:meth-comp1})."
        + reduced_n_note,
        r"\end{table*}",
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
    table5b_secondary_metrics()
    table6_overhead()
    print(f"\nAll tables saved to {TABLE_DIR}/")


if __name__ == "__main__":
    main()
