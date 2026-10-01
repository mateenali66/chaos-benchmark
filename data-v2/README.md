# data-v2

Raw experiment output. This directory is not in git. The data is on Zenodo (concept DOI [10.5281/zenodo.22004603](https://doi.org/10.5281/zenodo.22004603), CC BY 4.0) as three archives:

| Archive | Contents |
|---|---|
| `chaos-benchmark-data-bench-a.zip` | Components 1 and 2 from cluster `bench-a` (Chaos Mesh reps 1-15, LitmusChaos reps 16-30, Chaos Mesh overhead runs) |
| `chaos-benchmark-data-bench-b.zip` | Components 1 and 2 from cluster `bench-b` (LitmusChaos reps 1-15, Chaos Mesh reps 16-30, LitmusChaos overhead runs) |
| `chaos-benchmark-data-ml.zip` | Component 3 campaigns and Component 4 hypothesis samples |

Paths inside each archive start with `data-v2/`. Unzip them in the repository root.

## Layout

```
data-v2/
  bench-a/, bench-b/
    {chaos-mesh,litmus}/{scenario}/run-N.json            Component 1 (12 scenarios, 15 runs per tool per cluster)
    {chaos-mesh,litmus}/{scenario}/run-N.timeseries.json.gz
    overhead/baseline/run-N.json                          Component 2, no tool installed
    overhead/{tool}-idle/run-N.json                       Component 2, tool installed, no fault
    overhead/{tool}-fault/run-N.json                      Component 2, tool installed, scenario p1
    overhead/*/run-N.timeseries.json.gz
    overhead-tainted-128Mi/, overhead-noreset-200rps/     excluded pilot runs (not analysed)
    *.log                                                 batch run logs
  ml/
    campaigns/{arm}/campaign-N/injection-{1..10}.json     Component 3 (5 arms x 10 campaigns)
    campaigns/{arm}/campaign-N/injection-{1..10}.timeseries.json.gz
    campaigns/{arm}/campaign-N/campaign-summary.json
    campaigns/{arm}/campaign-N/llm-transcript.jsonl       LLM arms only
    hypotheses/{arm}/candidate-CNN/sample-{1..5}.json     Component 4 (3 arms x 36 candidates x 5 samples)
    campaigns-output.log, campaigns-progress.log
```

All 780 Component 1 and 2 runs have a sidecar. 490 of the 500 Component 3 injections have one.

Scenario IDs (`p1` to `a2`) are defined in `experiments/scenarios.yaml`, and candidate IDs (`C01` to `C36`) in `experiments/fault-space.yaml`. Arms are `random`, `coverage`, `llm-claude`, `llm-llama` and `llm-mistral` (`llm-*` only under `hypotheses/`).

The two excluded directories hold pilot overhead runs: `overhead-tainted-128Mi/` under the chart's default 128Mi memory limit, and `overhead-noreset-200rps/` without per-run state reset at 200 rps (`analysis/PREREGISTRATION.md`, Component 1 amendment items 2 to 4). No analysis script reads them.

## Run files (`run-N.json`, `injection-N.json`)

| Key | Contents |
|---|---|
| `metadata` | Tool, scenario or candidate, run number, UTC timestamp, cluster and phase durations. Overhead runs add `mode` and `overhead_config`. Campaign injections add `strategy`, `campaign`, `injection`, `candidate_id`, `scenario_template`, `category`, `target_service` and `param`. |
| `wrk2` | `throughput_rps`, `latency_ms` (`mean`, `p50`, `p95`, `p99`, `p999`), `errors` (`connect`, `read`, `write`, `timeout`, `http_non2xx3xx`), `requests_total`, `duration_s` and the end of the raw wrk2 output. One wrk2 job covers baseline, fault and recovery, so these are whole-window values. |
| `phases` | `start` and `end` (Unix seconds) and `infra_metrics` (Prometheus range results at a 15 s step for per-pod CPU, memory, restarts and network receive bytes) for `baseline`, `fault` and `recovery`. Overhead `baseline` and `idle` runs have a single `load` phase. |
| `derived` | `pod_restarts_during_fault`, `cpu_spike_pct`, `memory_spike_mb`. |
| `weakness_signals` | Campaign injections only. The four Component 3 signals and `any_violation` (`scripts/CAMPAIGN_MODES.md`). |

124 Component 1 runs have empty `infra_metrics` because the summary query failed. Their sidecars exist (`analysis/PREREGISTRATION.md`, Component 1 amendment item 8, and `data/exclusions.log`).

## Timeseries sidecars (`*.timeseries.json.gz`)

Gzipped JSON with `metadata` (`window_start`, `window_end`, `fault_start`, `fault_end` in Unix seconds, `step` of 5 s, `namespace`, and the PromQL `queries`) and `metrics`. Each entry in `metrics` holds the `query` and the Prometheus matrix `result`: container CPU, memory working set, network receive and transmit bytes, pod restarts, node CPU utilization and node memory used. The three HTTP series are always empty. `fault_start` and `fault_end` are `null` for overhead `baseline` and `idle` runs. Container-level series cover the namespace in `metadata.namespace`. See `scripts/SLOT_PARALLELISM.md` for sidecars written by slots 1 and 2.

## Campaign files

`campaign-summary.json` holds the campaign metadata (tool, strategy, seed, K, fault-space size), `injections_completed`, `violation_counts` and one entry per injection with its candidate and weakness signals.

`llm-transcript.jsonl` has one JSON object per model call with the prompt, raw completion, raw API response, model ID, validation outcome, selected candidate, rationale and whether the seeded fallback was used.

## Hypothesis samples (`sample-N.json`)

Each sample holds `metadata` (arm, Bedrock model ID, candidate, sample number, temperature, max tokens, generation time), `baseline_summary` (the no-fault baseline given to the model), `candidate`, `attempts` (each prompt, raw response and validation result), `prediction` (`throughput_direction`, `degraded_services`, `weakness_signal_present`, `rationale`) and `valid`.

## Logs

The `*.log` files are the console output of the batch drivers. In the 2026-09-30 deposit, local path prefixes in these logs were rewritten to paths relative to the repository root.
