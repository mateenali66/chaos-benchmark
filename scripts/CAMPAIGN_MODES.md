# Run modes

How to run the three run modes used for data collection:

- `run-experiment.py --mode benchmark` (Component 1), batch driver `run-all-experiments.sh`
- `run-experiment.py --mode overhead` (Component 2), batch driver `run-overhead.sh`
- `run-campaign.py` (Component 3), batch driver `run-all-campaigns.sh`

All three use the protocol code in `scripts/chaoslib.py`. Running three isolated copies of the application per cluster is covered in `scripts/SLOT_PARALLELISM.md`. Component 4 generation (`run-hypothesis-generation.py`) is documented in its module docstring.

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `CHAOS_DATA_DIR` | none, required | Output root, e.g. `data-v2/bench-a`. `chaoslib.py`, `run-all-experiments.sh` and `run-overhead.sh` exit if it is unset. `run-all-campaigns.sh` defaults it to `data-v2/ml`. |
| `CHAOS_ECR_REPO` | placeholder that does not resolve | wrk2 image repository. Set by `campaign-env.sh` and `run-all-campaigns.sh` to the image pushed by `build-wrk2-image.sh`. |
| `CHAOS_LOAD_RPS` | `120` | Offered load (wrk2 `-R`). |
| `CHAOS_WARMUP_S` | `120` | Length in seconds of the excluded warm-up before each run. `0` disables it. |
| `CHAOS_RESET_STATE` | `1` | The batch drivers and `run-campaign.py` run `reset-app-state.sh` before each run or injection. `0` disables it. |
| `CHAOS_SLOT` | `0` | Slot (namespace) to run in. See `SLOT_PARALLELISM.md`. |
| `CHAOS_PROM_PORT` | `9090` | Local port for the Prometheus port-forward, so runners for two clusters can share one workstation. |
| `CHAOS_CLUSTER_NAME` | basename of `CHAOS_DATA_DIR` | Value written to `metadata.cluster`. |
| `CHAOS_ASSUME_YES` | `0` | `1` makes `run-overhead.sh` skip its confirmation prompts. |
| `CHAOS_BENCHMARK_SKIP_CHAOSCENTER_VERIFY` | unset | Set to skip the ChaosCenter catalog check in `run-campaign.py`. |
| `CHAOS_HYPOTHESIS_DIR` | `data-v2/ml/hypotheses` | Output root for `run-hypothesis-generation.py`. |
| `SETUP_TOOLS` | per cluster | Chaos tools that `setup.sh` installs (`none`, `chaos-mesh`, `litmus`, `both`). |
| `INSTALL_LITMUS` | `1` | `0` makes `post-deploy.sh` skip the Litmus operator, CRDs and ChaosExperiments. |
| `AWS_PROFILE`, `AWS_REGION` | `default`, `ca-central-1` | AWS credentials and region. |

`source scripts/campaign-env.sh` sets `AWS_PROFILE`, `AWS_REGION`, `CHAOS_ECR_REPO` and `CHAOS_LOAD_RPS=120`.

## 1. Benchmark mode (Component 1)

Each run is a state reset, a 120 s warm-up, then baseline 300 s, fault 120 s, recovery 60 s and cooldown 60 s. One wrk2 job spans baseline, fault and recovery (480 s), so wrk2 throughput and latency are whole-window figures. Per-phase CPU, memory, restarts and network come from Prometheus.

```
./scripts/run-all-experiments.sh [--tool chaos-mesh|litmus|all] [--reps N] [--start-rep N] [--scenarios p1,p2,...] [--slot N]
```

`--reps` is the last repetition and `--start-rep` the first (defaults 30 and 1). The crossover was run as:

```
bench-a$ ./scripts/run-all-experiments.sh --tool chaos-mesh --reps 15
bench-a$ ./scripts/run-all-experiments.sh --tool litmus --start-rep 16 --reps 30
bench-b$ ./scripts/run-all-experiments.sh --tool litmus --reps 15
bench-b$ ./scripts/run-all-experiments.sh --tool chaos-mesh --start-rep 16 --reps 30
```

After the first few runs, each command was split across three slots with `--scenarios` and `--slot` (see `SLOT_PARALLELISM.md`). A run is skipped if its output file already exists, so an interrupted batch can be restarted with the same command.

Single run:

```
python3 scripts/run-experiment.py --tool chaos-mesh --scenario p1 --run 1
python3 scripts/run-experiment.py --tool litmus --scenario n3 --run 30 --dry-run
```

`--run` accepts 1 to 30. Each run writes `${CHAOS_DATA_DIR}/{tool}/{scenario}/run-N.json` and the sidecar `run-N.timeseries.json.gz` (section 4).

## 2. Overhead mode (Component 2)

| Config | Cluster state | Load |
|---|---|---|
| `baseline` | no chaos tool installed | 300 s, no fault |
| `idle` | `--tool` installed and running | 300 s, no fault |
| `fault` | `--tool` installed | full benchmark protocol for `--scenario` (default `p1`) |

Each run starts with a state reset and the 120 s warm-up.

```
./scripts/run-overhead.sh --tool chaos-mesh|litmus [--reps N] [--scenario ID] [--stage baseline|idle|fault|all]
```

`--reps` defaults to 10 and `--stage` to `all`. Before the `baseline` stage the script exits if any of the namespaces `chaos-mesh`, `chaos-testing` or `litmus` exists. With `--stage all` it pauses after `baseline` so the tool can be installed. A sequence that passes these checks on `bench-a`:

1. `SETUP_TOOLS=none ./scripts/setup.sh bench-a`, then delete the empty `chaos-testing` and `litmus` namespaces that `setup.sh` creates.
2. `./scripts/deploy-dsb.sh` and `INSTALL_LITMUS=0 ./scripts/post-deploy.sh`.
3. `./scripts/run-overhead.sh --tool chaos-mesh`. At the pause, run `SETUP_TOOLS=chaos-mesh ./scripts/setup.sh bench-a`, then confirm.

Output files, which are skipped if they already exist:

```
${CHAOS_DATA_DIR}/overhead/baseline/run-N.json
${CHAOS_DATA_DIR}/overhead/{tool}-idle/run-N.json
${CHAOS_DATA_DIR}/overhead/{tool}-fault/run-N.json
```

Single run:

```
python3 scripts/run-experiment.py --mode overhead --overhead-config baseline --run 1
python3 scripts/run-experiment.py --mode overhead --overhead-config idle --tool chaos-mesh --run 1
python3 scripts/run-experiment.py --mode overhead --overhead-config fault --tool chaos-mesh --scenario p1 --run 1
```

`--run` accepts 1 to 10.

## 3. Campaign mode (Component 3)

A campaign is K = 10 injections chosen one at a time from the 36 candidates in `experiments/fault-space.yaml`. Each injection is a state reset, a 120 s warm-up, then baseline 120 s, fault 120 s and recovery 60 s, with no cooldown.

```
python3 scripts/run-campaign.py --tool chaos-mesh|litmus --strategy STRATEGY --campaign N [--seed S] [--dry-run]
```

| Strategy | How it picks the next candidate |
|---|---|
| `random` | Shuffles the fault space with `--seed` (required) and takes candidates in order, so there are no repeats within a campaign. |
| `coverage` | Cycles through the categories (`application`, `network`, `pod`, `resource`) and takes the first untried (category, service) pair in the current category. Once every pair is tried it takes the least-tried pair. No randomness. |
| `llm-claude`, `llm-llama`, `llm-mistral` | Asks the Bedrock model in `experiments/llm-config.yaml`, using `scripts/prompts/selection.txt`, the topology summary and the campaign's history so far. An answer that is not valid JSON, names an unknown candidate or repeats one is re-prompted, up to 3 attempts in total. If all fail, the strategy takes a random untried candidate (seed `--seed`, or the campaign number) and counts a fallback. Every request and response is appended to `llm-transcript.jsonl`. |

Injection manifests are rendered from (`scenario_template`, `target_service`, `param`) by `render_chaos_mesh_manifest` and `render_litmus_manifest` in `run-campaign.py` and applied with `kubectl`. For LLM arms with `--tool litmus`, the script first checks the candidate list against ChaosCenter (`experiments/chaoscenter-experiment-ids.json`, `litmus-credentials.json`). The check only prints a warning on failure and does not affect execution.

Batch driver, one process per slot:

```
CHAOS_SLOT=0 ./scripts/run-all-campaigns.sh
CHAOS_SLOT=1 ./scripts/run-all-campaigns.sh
CHAOS_SLOT=2 ./scripts/run-all-campaigns.sh
```

It takes no flags. It runs all 50 (arm, campaign) pairs with `--tool litmus`, assigns pair `i` (arm index x 10 + campaign - 1) to slot `i mod 3`, gives the random arm seed = campaign number, and skips campaigns whose summary already reports 10 injections. `run-campaign.py` also skips injections whose file exists and rebuilds the campaign history from them, so a campaign can be restarted with the same arguments.

### Weakness signals

Computed per injection by `compute_weakness_signals()` in `run-campaign.py`.

| Signal | True when |
|---|---|
| `error_rate_violation` | wrk2 errors (connect, read, write, timeout, non-2xx/3xx) exceed 5% of requests. |
| `p99_over_3x_baseline` | Whole-window p99 exceeds 3 x the median p99 of the campaign's earlier injections. `None` when there is no earlier injection. |
| `recovery_over_60s` | Container CPU at the end of the 60 s recovery phase exceeds 1.2 x its baseline-phase mean. This is not a measured recovery time. |
| `pod_restarts_violation` | `derived.pod_restarts_during_fault > 0`. |
| `any_violation` | Any of the four above is true. |

### Output

```
${CHAOS_DATA_DIR}/campaigns/{strategy}/campaign-N/injection-{1..10}.json
${CHAOS_DATA_DIR}/campaigns/{strategy}/campaign-N/injection-{1..10}.timeseries.json.gz
${CHAOS_DATA_DIR}/campaigns/{strategy}/campaign-N/campaign-summary.json
${CHAOS_DATA_DIR}/campaigns/{strategy}/campaign-N/llm-transcript.jsonl   (LLM arms)
```

`campaign-summary.json` is rebuilt from the injection files on every invocation.

## 4. Timeseries sidecar (all modes)

Every run and injection also writes `<name>.timeseries.json.gz` next to its JSON file (`chaoslib.write_timeseries_sidecar`). The window runs from the end of the warm-up (or baseline start minus 60 s when the warm-up is disabled) to the end of the last phase. Component 1 sidecars were collected with a fixed 60 s pre-baseline buffer that overlaps the end of the warm-up (`analysis/PREREGISTRATION.md`, Component 1 amendment item 5).

The series are 5 s Prometheus range queries (`chaoslib.TIMESERIES_QUERIES`) for per-pod container CPU, memory working set, network receive and transmit bytes and restart count, plus node CPU utilization and node memory used. Three HTTP series use Envoy metric names and are empty because no service mesh is installed. `metadata.fault_start` and `metadata.fault_end` mark the fault window and are `null` for overhead `baseline` and `idle` runs. A failed query is stored with an `error` key or an empty `result`. If Prometheus is unreachable the sidecar is skipped and the run still succeeds.

Schema:

```json
{
  "metadata": {
    "window_start": 1234567890.0,
    "window_end": 1234568500.0,
    "fault_start": 1234568000.0,
    "fault_end": 1234568120.0,
    "step": "5s",
    "namespace": "social-network",
    "queries": {"container_cpu_usage": "...", "...": "..."}
  },
  "metrics": {
    "container_cpu_usage": {"query": "...", "result": ["Prometheus matrix result"]},
    "...": "..."
  }
}
```
