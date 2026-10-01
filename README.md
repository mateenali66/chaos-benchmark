# Chaos Engineering Benchmark

Code, configuration and analysis for the study "Machine Learning for Chaos Engineering: A Taxonomy and a Three-Phase Empirical Evaluation of Fault Selection, Hypothesis Generation, and Impact Detection" (Mateen Ali Anjum, Phono Technologies Inc., Kitchener, ON, Canada).

The study runs five components against the [DeathStarBench](https://github.com/delimitrou/DeathStarBench) Social Network application on AWS EKS. The raw data is on Zenodo (see [Data](#data)).

The statistical plan, every dated amendment and the reasons for each are in [`analysis/PREREGISTRATION.md`](analysis/PREREGISTRATION.md). Read its 2026-09-30 erratum first. The plan was first committed after all Component 2 runs and the first 20 Component 1 runs had started, and the erratum lists the later deviations.

## Components

| # | Component | What it does |
|---|-----------|--------------|
| 1 | Tool benchmark | Chaos Mesh vs LitmusChaos on 12 fault scenarios, 30 runs per tool per scenario (720 runs), crossover across two clusters. |
| 2 | Overhead decomposition | Chaos Mesh only. Mean CPU and memory per pod in the application namespace (application pods and load generator, not the tool's own pods) with no tool, with the tool idle, and during a fault, 10 runs each. |
| 3 | Fault selection | 5 strategies (random, coverage heuristic, 3 LLMs) x 10 campaigns x 10 injections (500 injections) from a 36-candidate fault space. |
| 4 | Hypothesis generation | 3 LLMs predict each candidate's throughput impact from topology and baseline telemetry only (540 samples), scored against Component 3's measured outcomes. |
| 5 | Impact detection | EWMA, Isolation Forest, autoencoder and Deep SVDD, trained per run on the baseline phase of each run's telemetry, against a static-threshold baseline. Reported on the 240 slot-0 Component 1 runs. |

## Key results

The numbers below come from `analysis/results/`.

- In Component 1, 2 of 12 scenarios differ in throughput between tools after Holm correction: HTTP Abort 503 (median 77.5 rps Chaos Mesh vs 115.6 rps LitmusChaos, Cliff's delta -1.00) and Container Kill (119.6 vs 103.8 rps, delta +1.00). The other 10 do not.
- In Component 2, with Chaos Mesh installed and idle, application CPU and memory per pod do not change measurably (both 95% CIs include zero). During a fault they rise by 0.0093 cores and 2.41 MB per pod (both CIs exclude zero). LitmusChaos is not reported because it runs its runner and fault pods in the application namespace.
- In Component 3, the Kruskal-Wallis test across the 5 arms gives H = 24.39, p = 0.00007. The coverage heuristic finds fewer weakness classes than each of the other four arms (Holm-corrected p < 0.01). Random and the three LLM arms do not differ from each other. Without the recovery signal, which fires on 467 of 500 injections, the result is the same (post hoc).
- In Component 4, no LLM beats chance (balanced accuracy 0.5). Claude scores 0.469, Mistral 0.519 and Llama 0.500, because Llama predicts "degrade" for every candidate. The practitioner heuristic scores 0.575 against 0.5 for an always-majority baseline. No significance test is registered for this component.
- Component 5 uses only the 240 slot-0 runs (scenarios P1, P2, P3, N1), because the other runs' sidecars hold slot 0's telemetry (see the erratum). On the 96 of those runs where the static-threshold baseline is not constant, Isolation Forest (median AUC-ROC 0.872), the autoencoder (0.826) and Deep SVDD (0.810) beat the baseline (0.808), and EWMA (0.636) is worse (paired Wilcoxon, Holm-corrected). Medians over all 240 runs are 0.871, 0.821, 0.811 and 0.599.

In Components 3 to 5, the LLM approaches do not beat the simpler baselines (random selection, a majority-class rule, a static threshold). The three detectors that beat their baseline are not LLM-based.

## Setup

- AWS EKS 1.31 in `ca-central-1`, ON_DEMAND nodes, one instance type per cluster.
- `is-chaos-bench-a` and `is-chaos-bench-b` (m5.xlarge) ran Components 1 and 2. Each cluster ran both tools in counterbalanced order. Component 2 ran on 3 nodes with one copy of the application. All but 4 Component 1 runs ran on 9 nodes, almost all of them as three isolated copies of the application (slots, `scripts/SLOT_PARALLELISM.md`).
- `is-chaos-ml` (3 x m5.2xlarge, three slots) ran Component 3. Components 4 and 5 use the collected data and need no cluster.
- The application is DeathStarBench Social Network with the resource override in `helm/dsb-values.yaml`, monitored with kube-prometheus-stack and Jaeger. [wrk2](https://github.com/giltene/wrk2) offers 120 rps.
- Every run starts with an application-state reset (`scripts/reset-app-state.sh`) and a 120 s warm-up that is excluded from measurement. Phase durations are constants in `scripts/chaoslib.py`.
- `experiments/scenarios.yaml` lists Component 1's 12 scenarios. `experiments/fault-space.yaml` lists the 36 candidates used by Components 3 and 4 (12 fault templates x 3 target services).
- The LLM arms call Amazon Bedrock directly. Model IDs, requested temperatures and token limits are in `experiments/llm-config.yaml` (the Claude model rejects an explicit temperature, see the erratum), and the prompts are in `scripts/prompts/`.

## Repository layout

```
analysis/
  PREREGISTRATION.md          analysis plan, amendments, 2026-09-30 erratum
  analyze.py                  Component 1 tests, effect sizes, robustness views
  component2_analyze.py       Component 2
  component3_analyze.py       Component 3
  component5_slot0.py         Component 5 on slot-0 runs (the reported analysis)
  component5_sensitivity.py   Component 5 post hoc sensitivity analysis (all runs)
  tables.py, figures.py       Component 1 tables and figures
  tables_figures_ml.py        Components 3, 4 and 5 tables and figures
  results/, tables/, figures/ generated outputs
data/exclusions.log           excluded runs (see the plan's exclusion rules)
data-v2/README.md             layout of the raw data (data-v2/ itself is not in git)
experiments/                  scenarios, fault space, LLM configuration, fault manifests
helm/, manifests/             Helm values and Kubernetes manifests
load-generator/               wrk2 image and workload script
scripts/
  chaoslib.py                 shared run protocol, wrk2, Prometheus and sidecar code
  run-experiment.py, run-all-experiments.sh, run-overhead.sh   Components 1 and 2
  run-campaign.py, run-all-campaigns.sh                        Component 3
  run-hypothesis-generation.py, score-hypotheses.py, practitioner_heuristic.py  Component 4
  component5_features.py, component5_detectors.py, component5_evaluate.py       Component 5
  setup.sh, deploy-dsb.sh, post-deploy.sh, teardown.sh, ...    cluster setup
  watchdogs/                  monitors for long-running batches
  CAMPAIGN_MODES.md, SLOT_PARALLELISM.md                       how to run the batches
terraform/                    VPC, EKS and add-ons, one workspace per cluster
DeathStarBench/               git submodule
```

## Reproducing the analysis

No cluster is needed. Download the three data archives from Zenodo and unzip them in the repository root so that `data-v2/bench-a`, `data-v2/bench-b` and `data-v2/ml` exist.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install numpy scipy pandas matplotlib seaborn pyyaml scikit-learn torch

python3 analysis/analyze.py               # Component 1
python3 analysis/component2_analyze.py    # Component 2
python3 analysis/component3_analyze.py    # Component 3
python3 scripts/score-hypotheses.py       # Component 4
python3 scripts/component5_evaluate.py    # Component 5 (about 2 minutes on an Apple Silicon CPU, no GPU)
python3 analysis/component5_sensitivity.py
python3 analysis/tables.py
python3 analysis/figures.py
python3 analysis/tables_figures_ml.py
```

Outputs are written to `analysis/results/`, `analysis/tables/` and `analysis/figures/`.

## Rerunning the experiments

This needs an AWS account with EKS and Amazon Bedrock access. The outline below is for `bench-a`. `scripts/CAMPAIGN_MODES.md` documents every flag and environment variable, and `scripts/SLOT_PARALLELISM.md` covers running three slots per cluster.

```bash
git submodule update --init --recursive
terraform -chdir=terraform init
terraform -chdir=terraform workspace new bench-a
terraform -chdir=terraform apply -var-file=envs/bench-a.tfvars

SETUP_TOOLS=both ./scripts/setup.sh bench-a   # monitoring, Chaos Mesh and LitmusChaos
./scripts/deploy-dsb.sh                       # DeathStarBench
./scripts/post-deploy.sh                      # Litmus RBAC and ChaosExperiments, social graph
./scripts/smoke-test.sh
./scripts/build-wrk2-image.sh
source scripts/campaign-env.sh                # region, wrk2 image, offered load
export CHAOS_DATA_DIR="$(pwd)/data-v2/bench-a"   # required, no default

./scripts/run-all-experiments.sh --tool chaos-mesh --reps 15               # Component 1, reps 1-15
./scripts/run-all-experiments.sh --tool litmus --start-rep 16 --reps 30    # reps 16-30

./scripts/teardown.sh bench-a
terraform -chdir=terraform destroy -var-file=envs/bench-a.tfvars
```

`bench-b` runs the tools in the opposite order. Component 2 needs the cluster without any chaos tool for its first stage, so it runs before the tools are installed (order in `scripts/CAMPAIGN_MODES.md`). Component 3 runs on the `ml` cluster with one `CHAOS_SLOT=<0|1|2> ./scripts/run-all-campaigns.sh` process per slot. Component 4 generation is `python3 scripts/run-hypothesis-generation.py`. `bench-a` creates the shared S3 bucket (`create_s3_bucket = true`), so destroy it last.

## Data

The raw data for all five components is on Zenodo under CC BY 4.0:

- Concept DOI [10.5281/zenodo.22004603](https://doi.org/10.5281/zenodo.22004603), which resolves to the latest version.

It holds three archives (`bench-a`, `bench-b`, `ml`). [`data-v2/README.md`](data-v2/README.md) describes their layout and file formats. The older concept DOI 10.5281/zenodo.20574917 holds a superseded pilot dataset and does not match this code.

## License

CC BY 4.0. See [LICENSE](LICENSE).

## Citation

```bibtex
@misc{anjum2026chaos,
  title  = {Machine Learning for Chaos Engineering: A Taxonomy and a
            Three-Phase Empirical Evaluation of Fault Selection, Hypothesis
            Generation, and Impact Detection},
  author = {Anjum, Mateen Ali},
  year   = {2026},
  note   = {Manuscript in preparation. Data: https://doi.org/10.5281/zenodo.22004603}
}
```
