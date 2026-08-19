# Chaos Engineering Benchmark

Reproducibility package for the paper (working title):

> **Machine learning for chaos engineering: an empirical study of fault-selection, hypothesis-generation, and impact-detection strategies on microservices**
>
> Mateen Ali Anjum, Phono Technologies Inc., Kitchener, ON, Canada
>
> In preparation for the *Journal of Systems and Software* (Elsevier), 2026. An earlier, narrower version of this manuscript (a 2-tool benchmark with no ML component) was rejected after full peer review by *Software: Practice and Experience* (Wiley) in August 2026; both reviewers' core objection was that the title promised machine learning while the empirical work contained none. This repository reflects the reworked study, which adds three genuine ML components (Components 3-5 below) built and run against the same infrastructure. The rejected version is preserved at `../archive/spe-rejected-2026-08-15/`.

This repository contains the infrastructure code, experiment definitions, orchestration scripts, raw data, and statistical analysis for five components run against a [DeathStarBench](https://github.com/delimitrou/DeathStarBench) Social Network microservices testbed on AWS EKS:

| # | Component | What it measures | Status |
|---|-----------|-------------------|--------|
| 1 | Tool benchmark | Chaos Mesh vs LitmusChaos, 12 fault scenarios x n=30 reps (720 runs) | Complete |
| 2 | Overhead decomposition | Standing chaos-agent overhead vs fault side effects, 3 configs x 10 reps | Complete for Chaos Mesh; LitmusChaos data lost to a Prometheus collection gap (disclosed) |
| 3 | Fault-selection strategy | 5 arms (random, coverage heuristic, 3 LLMs) x 10 campaigns x K=10 injections (500 injections) | Complete |
| 4 | LLM hypothesis generation | 3 LLMs predict fault impact from topology + baseline telemetry alone, scored against real ground truth (36 candidates) | Complete |
| 5 | ML impact detection | EWMA / Isolation Forest / autoencoder / Deep SVDD vs a static-threshold baseline, trained per-run on Component 1's 720 sidecars | Complete |

Every component's exact statistical methodology (metrics, tests, corrections, and any mid-study amendment with the reason it was made) is pre-registered and dated in [`analysis/PREREGISTRATION.md`](analysis/PREREGISTRATION.md) -- read that file for the authoritative methods description; this README summarizes it.

## Key Results

**Component 1** (n=30/cell, Mann-Whitney U, Holm-Bonferroni within metric family): 2 of 12 scenarios show a significant, large-effect throughput difference between tools after correction -- HTTP Abort 503 (Chaos Mesh 77.5 vs LitmusChaos 115.6 rps, Cliff's d = -1.00) and Container Kill (Chaos Mesh 119.6 vs LitmusChaos 103.8 rps, d = +1.00). The other 10 scenarios show no significant difference.

**Component 2**: Chaos Mesh's standing idle overhead is negligible (CPU and memory CIs both cross zero); its fault-phase side effects are small but real (CPU +0.0093 cores, memory +2.41 MB, both CIs exclude zero). LitmusChaos's overhead is unknown -- its entire 30-run overhead dataset has empty Prometheus metrics from the original collection, and both clusters are now torn down.

**Component 3** (Kruskal-Wallis across 5 arms, H=24.39, p=0.00007): the `coverage` heuristic is significantly worse at discovering unique weakness classes than all four other arms (large effects, Holm-corrected p<0.01 vs each). Random and all three LLM arms are statistically indistinguishable from each other -- LLM-driven fault selection does not significantly improve discovery over random selection.

**Component 4**: none of the three LLMs beat chance (0.5) at predicting fault impact from topology alone (Claude 0.469, Mistral 0.519, Llama 3 a degenerate constant predictor mechanically pinned at 0.5). A practitioner heuristic modestly beats a trivial always-predict-majority-class baseline (0.575 vs 0.5), but no pairwise significance test is registered for this component.

**Component 5**: on the 299-run paired subset used for the confirmatory test, Isolation Forest (median AUC-ROC 0.884) and a reconstruction autoencoder (0.849) significantly beat a static-threshold baseline (0.840, paired Wilcoxon, Holm-corrected p<0.001 both); Deep SVDD (0.811) does not differ significantly; EWMA (0.618) is significantly worse than the baseline. (Full-719-run descriptive medians, not directly comparable to the baseline: 0.883 / 0.827 / 0.805 / 0.582 respectively.)

The throughline across Components 3-5: LLM-driven approaches do not clearly beat simpler baselines anywhere in this study (random selection, a majority-class baseline, and a static threshold rule, respectively) -- the two detectors that do beat their baseline (Isolation Forest, autoencoder) are not LLM-based. This is reported as a finding, not smoothed over.

## Experimental Setup

### Infrastructure

- **Cluster**: AWS EKS 1.31, `ca-central-1`, ON_DEMAND `m5.xlarge` (Components 1/2) or `m5.2xlarge` (Component 3, upsized after a CPU-scheduling deadlock during 3-slot parallel campaigns)
- **Three clusters, all now torn down after data collection completed**: `is-chaos-bench-a` and `is-chaos-bench-b` ran Component 1's crossover design (each cluster runs both tools, counterbalanced) plus Component 2's overhead isolation runs; `is-chaos-ml` ran Component 3's 500-injection campaign (3-slot intra-cluster parallelism). Components 4 and 5 are analysis-phase only -- no cluster time, scored/trained against already-collected data.
- **Application**: DeathStarBench Social Network (27 microservices)
- **Monitoring**: Prometheus (kube-prometheus-stack) + Grafana + Jaeger; per-run gzipped Prometheus timeseries sidecars are the raw substrate for Component 5's detectors.

### Fault space

Component 1 uses the original 12 scenarios (`experiments/scenarios.yaml`). Components 3/4 use an expanded 36-candidate fault space (`experiments/fault-space.yaml`) that crosses each of the 12 fault templates against up to 3 target services.

### Protocol

Every run: a full application-state reset (`scripts/reset-app-state.sh`), then an excluded warm-up, then baseline load, fault injection, recovery, and (for Component 1/2) a cooldown phase. Exact durations per component are frozen constants (`chaoslib.py` / `run-campaign.py`) and documented in `analysis/PREREGISTRATION.md`. Load is generated by [wrk2](https://github.com/giltene/wrk2).

## Repository Structure

```
chaos-benchmark/
├── analysis/
│   ├── PREREGISTRATION.md         # Authoritative methods + all dated amendments -- read this first
│   ├── analyze.py                 # Component 1: Mann-Whitney U, Holm-Bonferroni, Cliff's delta+BCa CI
│   ├── component2_analyze.py      # Component 2: overhead decomposition
│   ├── component3_analyze.py      # Component 3: discovery-curve AUC, Kruskal-Wallis
│   ├── tables.py, figures.py      # Component 1 tables/figures (read analyze.py's CSVs only)
│   ├── tables_figures_ml.py       # Components 3/4/5 tables/figures
│   ├── figures/, tables/          # Generated outputs (PDF + PNG, .tex)
│   └── results/                   # CSV/JSON outputs -- the single source of truth per component
├── data-v2/                       # Current dataset (superseded a Feb-era data/ with an SPE-flagged
│   │                               # SPOT/mixed-instance confound, kept archived, never mixed in)
│   ├── bench-a/, bench-b/         # Component 1 (crossover) + Component 2 (overhead) runs
│   └── ml/campaigns/              # Component 3's 500 injections (5 arms x 10 campaigns x 10)
│       ml/hypotheses/             # Component 4's 540 LLM prediction samples
├── experiments/
│   ├── scenarios.yaml             # Component 1's 12 scenarios
│   └── fault-space.yaml           # Components 3/4's 36-candidate fault space
├── scripts/
│   ├── chaoslib.py                # Shared protocol/metrics library
│   ├── run-all-experiments.sh, run-overhead.sh   # Component 1/2 orchestration
│   ├── run-campaign.py, run-all-campaigns.sh     # Component 3 fault-selection campaigns
│   ├── run-hypothesis-generation.py, score-hypotheses.py, practitioner_heuristic.py  # Component 4
│   ├── component5_features.py, component5_detectors.py, component5_evaluate.py       # Component 5
│   ├── litmus_chaoscenter_client.py, register-chaoscenter-experiments.py  # ChaosCenter integration
│   └── watchdogs/                 # Long-running campaign monitors (stall/error/cluster-health detection)
├── terraform/                     # EKS infrastructure (VPC, EKS, addons), 3 workspaces
└── DeathStarBench/                # Vendored source (wrk2 build context)
```

## Reproducing the Analysis (no cluster required)

Raw data is not committed to this repository (`data-v2/` is gitignored; the JSON/timeseries files are large per-run outputs, not source). It is archived separately on Zenodo (see Data Availability below) -- download and extract it to `data-v2/` before running the commands below. To regenerate every table, figure, and statistical result from scratch:

```bash
cd chaos-benchmark
python3 -m venv .venv && source .venv/bin/activate
pip install numpy scipy pandas matplotlib seaborn pyyaml scikit-learn torch

cd analysis
python3 analyze.py              # Component 1
python3 component2_analyze.py   # Component 2
python3 component3_analyze.py   # Component 3
cd ../scripts
python3 score-hypotheses.py     # Component 4
python3 component5_evaluate.py  # Component 5 (~2 min on Apple Silicon CPU; no GPU needed)
cd ../analysis
python3 tables.py               # Component 1 LaTeX tables
python3 figures.py              # Component 1 figures
python3 tables_figures_ml.py    # Components 3/4/5 tables + figures
```

## Reproducing the Full Campaign (requires re-provisioning infrastructure)

`CHAOS_DATA_DIR` has no default and must always be set explicitly (a missing/wrong default here silently misdirected several runs during development -- see git history on `scripts/chaoslib.py`).

```bash
cd terraform && terraform init
terraform workspace new bench-a && terraform apply -var-file=envs/bench-a.tfvars
cd .. && ./scripts/setup.sh bench-a && ./scripts/post-deploy.sh && ./scripts/smoke-test.sh
./scripts/build-wrk2-image.sh

export CHAOS_DATA_DIR="$(pwd)/data-v2/bench-a"
./scripts/run-all-experiments.sh --tool chaos-mesh --reps 30    # Component 1
./scripts/run-overhead.sh                                        # Component 2

export CHAOS_DATA_DIR="$(pwd)/data-v2/ml"
./scripts/run-all-campaigns.sh --tool litmus                     # Component 3

./scripts/teardown.sh bench-a
terraform -chdir=terraform destroy -var-file=envs/bench-a.tfvars
```

Tear down `bench-a` last if it owns the shared S3 artifacts bucket (`create_s3_bucket = true` in its tfvars).

## Data Format

Each Component 1/2/3 run/injection produces a JSON file with `metadata` (tool/scenario/timestamps/protocol), `wrk2` (throughput, latency percentiles, error counts), `phases` (per-phase start/end + Prometheus `infra_metrics`), and `derived` (pod restarts, CPU/memory spike). Component 1 runs additionally have a gzipped `.timeseries.json.gz` sidecar: 5-second-step Prometheus series (container CPU/memory/network, pod restarts, node CPU/memory) spanning baseline through recovery -- Component 5's raw substrate. Component 4 samples are per-(model, candidate) JSON predictions in `data-v2/ml/hypotheses/`.

## Statistical Methods

- **Component 1**: two-sided Mann-Whitney U per scenario (unpaired), Holm-Bonferroni within each metric family, Cliff's delta with 95% BCa bootstrap CIs (10,000 resamples; percentile fallback where BCa is degenerate under perfect separation).
- **Component 2**: descriptive only, bootstrap median differences with 95% percentile CIs; no significance test is registered.
- **Component 3**: Kruskal-Wallis across the 5 arms on discovery-curve AUC; if significant, pairwise Mann-Whitney U with Holm correction and Cliff's delta.
- **Component 4**: balanced accuracy with 95% percentile bootstrap CIs; descriptive only, no significance test registered.
- **Component 5**: AUC-ROC per detector with 95% percentile bootstrap CIs across runs; paired Wilcoxon signed-rank vs the static-threshold baseline, Holm-corrected across the 4 detectors.

Full detail, including every mid-study amendment and why it was made, is in `analysis/PREREGISTRATION.md`.

## License

CC BY 4.0. See [LICENSE](LICENSE) for details.
