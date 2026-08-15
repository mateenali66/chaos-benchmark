# Run Modes: benchmark, overhead, campaign

This document covers `run-experiment.py`'s two modes (`benchmark`, `overhead`),
the batch drivers around them (`run-all-experiments.sh`, `run-overhead.sh`),
and the campaign scaffold (`run-campaign.py`). It does not cover
`terraform/`, `setup.sh`, `teardown.sh`, `export-to-s3.sh`, or
`scripts/watchdogs/` (owned elsewhere) or the top-level `README.md`.

Shared library: `scripts/chaoslib.py` holds every kubectl/wrk2/Prometheus
helper and the two protocol executors (`run_fault_protocol`,
`run_flat_load`) that all three modes call into. It exists mainly because
`run-experiment.py`'s hyphenated filename can't be `import`-ed directly by
`run-campaign.py` without `importlib` tricks.

---

## 1. `--mode benchmark` (default) -- the original 4-phase protocol at n=30

Unchanged protocol: BASELINE 300s / FAULT 120s / RECOVERY 60s / COOLDOWN 60s,
per (tool, scenario, rep). Only the repetition count changed, from 5 to 30,
and the run is now split across two identical clusters instead of one:

```
bench-a$ ./scripts/run-all-experiments.sh --tool chaos-mesh --reps 30
bench-b$ ./scripts/run-all-experiments.sh --tool litmus --reps 30
```

That's 2 tools x 12 scenarios x 30 reps = 720 total runs, 360 per cluster.
`--tool all` (the default) runs both tools sequentially on one cluster if
you don't have the two-cluster layout available.

`--start-rep N` skips ahead in the rep loop without re-checking every file
below N (an optimization on top of normal resume, not a replacement for it):

```
bench-a$ ./scripts/run-all-experiments.sh --tool chaos-mesh --reps 30 --start-rep 14
```

**Resume semantics**: per (tool, scenario, rep), based on whether
`data/{tool}/{scenario}/run-{rep}.json` already exists -- identical to the
original script. This is safe on a cluster that only has one tool's data,
because the resume check only ever looks at files under the `--tool` you
passed; bench-a re-running `--tool chaos-mesh` never looks at (or needs)
litmus's files from bench-b.

Single-run form (what the batch script calls):

```
python3 scripts/run-experiment.py --tool chaos-mesh --scenario p1 --run 1
python3 scripts/run-experiment.py --tool litmus --scenario n3 --run 30 --dry-run
```

`--run` now accepts 1-30 (was 1-10 for n=5's old headroom).

Output: `data/{tool}/{scenario}/run-N.json`, schema unchanged from before
this revision -- the original 120-run dataset stays reproducible bit-for-bit
against the same code paths, now just wrapped in `chaoslib`. Every run also
gets a timeseries sidecar (see section 4) alongside the run JSON; the
sidecar is new, the run JSON itself is not.

---

## 2. `--mode overhead` -- overhead-isolation study (3 configs x 10 reps)

Separates "cost of running the chaos tool" from "cost of the fault itself"
by measuring three configurations, always in this order:

| Config | What's running | Fault? | Load window |
|---|---|---|---|
| `baseline` | nothing (no chaos-mesh, no litmus) | no | 300s |
| `idle` | `--tool`'s agents installed and running | no | 300s |
| `fault` | `--tool`'s agents installed | yes (`--scenario`, default `p1`) | full 4-phase protocol (300/120/60/60) |

### Output path naming -- a deliberate deviation from the literal brief

The brief said `data/overhead/{config}/run-N.json`. In practice `idle` and
`fault` measurements are meaningless without knowing which tool was
installed, and running the study for both chaos-mesh and litmus (which you
will want, to compare their overheads) would otherwise silently overwrite
one tool's `idle`/`fault` data with the other's. So the config folder name
folds in `--tool` for those two configs, and `baseline` (which by
definition has no tool installed) stays tool-agnostic and is only ever
measured once:

```
data/overhead/baseline/run-{1..10}.json
data/overhead/chaos-mesh-idle/run-{1..10}.json
data/overhead/chaos-mesh-fault/run-{1..10}.json
data/overhead/litmus-idle/run-{1..10}.json
data/overhead/litmus-fault/run-{1..10}.json
```

### Batch driver: `run-overhead.sh`

Enforces the required ordering -- all `baseline` reps must run before the
tool is installed, or the measurement is contaminated -- via:

1. A `kubectl get namespace chaos-mesh` / `litmus` check before the
   `baseline` stage; it refuses to run if either namespace exists.
2. An interactive `y/N` confirmation before every stage transition,
   including an explicit pause between `baseline` and `idle` so you can
   install the tool by hand in between (tool install is owned by
   `setup.sh`, not automated here).

```
./scripts/run-overhead.sh --tool chaos-mesh                     # all 3 stages, 10 reps each
./scripts/run-overhead.sh --tool chaos-mesh --stage baseline     # just stage (a)
./scripts/run-overhead.sh --tool litmus --stage idle --reps 10   # just stage (b)
./scripts/run-overhead.sh --tool litmus --stage fault --scenario p1
```

Three-cluster layout (if the overhead study gets its own cluster rather
than reusing bench-a/bench-b): `overhead-c` runs
`./scripts/run-overhead.sh --tool chaos-mesh` then, after a clean teardown
and a fresh baseline is not needed a second time (baseline is tool-agnostic,
see above), `./scripts/run-overhead.sh --tool litmus --stage idle` +
`--stage fault` reusing the same `baseline/` data already collected.

**Resume semantics**: identical mechanism to `--mode benchmark`, scoped by
config-label folder instead of `(tool, scenario)` -- `run-overhead.sh` and
`run-experiment.py --mode overhead` both skip any `run-N.json` that already
exists under the relevant `data/overhead/{config_label}/` folder. Re-running
`--stage all` after a partial run is safe: `baseline` will refuse to re-run
only if the tool got installed in the meantime (namespace check), otherwise
every stage just resumes at the first missing rep.

Single-run form:

```
python3 scripts/run-experiment.py --mode overhead --overhead-config baseline --run 1
python3 scripts/run-experiment.py --mode overhead --overhead-config idle --tool chaos-mesh --run 1
python3 scripts/run-experiment.py --mode overhead --overhead-config fault --tool chaos-mesh --scenario p1 --run 1
```

`--run` accepts 1-10 in overhead mode.

---

## 3. Campaign mode -- ML fault-selection study scaffold (`run-campaign.py`)

A campaign is a sequence of K=10 fault injections chosen one at a time from
the 36-candidate fault space in `experiments/fault-space.yaml`, run with a
shortened per-injection protocol: BASELINE 120s / FAULT 120s / RECOVERY 60s,
**no cooldown**. This keeps a 10-injection campaign to roughly 50 minutes of
cluster time (10 x 300s) instead of the ~9 hours a 10x full-protocol run
would take.

```
python3 scripts/run-campaign.py --tool chaos-mesh --strategy random --seed 42 --campaign 1
python3 scripts/run-campaign.py --tool litmus --strategy coverage --campaign 1
python3 scripts/run-campaign.py --tool chaos-mesh --strategy random --seed 42 --campaign 1 --dry-run
```

### Fault space: 36 candidates

`experiments/fault-space.yaml` extends the original 12 scenario templates
(same taxonomy as `experiments/scenarios.yaml`: 3 pod, 4 network-shape x
3 latency tiers + loss + partition = 5, 2 resource, 2 application -- 12
templates total) across the 10 available DeathStarBench social-network
services, 3 target services per template, assigned by continuous
round-robin so exposure is roughly even (services early in the round-robin
list get 4 appearances across the 36 candidates, the rest get 3):

| Template | Category | Services (round-robin) |
|---|---|---|
| pod-kill | pod | compose-post, user, social-graph |
| container-kill | pod | post-storage, home-timeline, user-timeline |
| pod-failure | pod | media, url-shorten, unique-id |
| latency-50ms | network | nginx-thrift, compose-post, user |
| latency-100ms | network | social-graph, post-storage, home-timeline |
| latency-300ms | network | user-timeline, media, url-shorten |
| packet-loss-5pct | network | unique-id, nginx-thrift, compose-post |
| network-partition | network | user, social-graph, post-storage |
| cpu-stress-80pct | resource | home-timeline, user-timeline, media |
| memory-pressure-80pct | resource | url-shorten, unique-id, nginx-thrift |
| http-abort-503 | application | compose-post, user, social-graph |
| grpc-unavailable | application | post-storage, home-timeline, user-timeline |

8 of the 10 services were already targeted by the original 12 benchmarked
scenarios; only `url-shorten-service` and `unique-id-service` are genuinely
new targets. Reusing a service under a *different* fault template than the
original benchmark used is intentional (a real fault-injection catalog
would do the same), not an oversight.

**Known limitation, disclosed in `fault-space.yaml`**: the `http-abort-503`
and `grpc-unavailable` `port` params are inherited from the two services the
original benchmark actually validated (nginx-thrift:8080, user-service:9090)
and are placeholders for the other 8 services. Verify actual container
ports before running an application-category candidate against a service
other than those two.

### Strategies

`Strategy.select(history, fault_space, k_remaining) -> FaultCandidate`,
called once per injection with everything completed in the campaign so far.

- **`random`** (seeded): shuffles the full fault space once per seed and
  walks it in order; deterministic for a given seed, no repeats within a
  10-injection campaign (36 candidates > 10).
- **`coverage`** (deterministic): round-robins through categories
  (`application, network, pod, resource`, alphabetical) and picks the first
  untried `(category, service)` combination within whichever category's
  turn it is; falls back to the globally least-tried combination once
  everything has been tried at least once. No RNG involved.
- **`llm`** (stub): raises `NotImplementedError` with a TODO in its
  docstring. Design questions pending separate research before this can run
  for real: which model/MCP tool, what goes in the prompt (raw
  `weakness_signals`? the timeseries sidecar?), cost/latency budget per
  injection, determinism/reproducibility (pinned model id + transcript
  logging), and validating the model's chosen candidate id against
  `fault_space` before ever rendering a manifest from it. See
  `LLMStrategy`'s docstring in `run-campaign.py` for the full list.

### Manifest rendering

Unlike `--mode benchmark`/`overhead`, which apply pre-existing
`experiments/{tool}/{scenario}.yaml` files, campaign injections are
rendered on the fly (`render_chaos_mesh_manifest` /
`render_litmus_manifest` in `run-campaign.py`) from
`(scenario_template, target_service, param)`, written to a temp
`_manifest-injection-N.yaml` in the campaign directory, applied via
`chaoslib.run_fault_protocol`, and deleted afterward. The rendering
patterns were reverse-engineered from the 12 existing
`experiments/{chaos-mesh,litmus}/*.yaml` files so the generated manifests
match the same Kind/experiment-name/param shapes chaos-mesh and litmus
already use in this repo.

### Weakness signals

Four SLO-violation flags per injection, computed in
`compute_weakness_signals()`:

- `error_rate_violation`: aggregate wrk2 error rate (connect+read+write+
  timeout+non-2xx3xx / total requests) > 5%.
- `p99_over_3x_baseline`: this injection's aggregate wrk2 p99 vs. the
  **running median of prior injections' p99 in the same campaign** (`None`,
  not a violation, on injection 1). This is an approximation, not a true
  within-injection baseline-phase-only comparison -- the shared single wrk2
  job spanning baseline+fault+recovery (same design as `--mode benchmark`)
  means there is no separate client-side latency measurement per phase.
  Extending the load generator to report phase-scoped latency is out of
  scope for this scaffold.
- `recovery_over_60s`: proxy for "hadn't settled by the end of the fixed
  60s recovery window" -- container CPU at the end of the recovery phase is
  still >20% above the baseline-phase average. Not a measured wall-clock
  time-to-recovery.
- `pod_restarts_violation`: `derived.pod_restarts_during_fault > 0`
  (same field `--mode benchmark` already computes).

`any_violation` is the OR of all four. These are exactly what a later
discovery-metrics pass (e.g. "how many distinct weaknesses did strategy X
surface in N injections") is meant to consume.

### Output

```
data/campaigns/{strategy}/campaign-{N}/injection-{1..10}.json
data/campaigns/{strategy}/campaign-{N}/injection-{1..10}.timeseries.json.gz
data/campaigns/{strategy}/campaign-{N}/campaign-summary.json
```

`campaign-summary.json` aggregates every injection's candidate identity,
weakness signals, and per-flag violation counts; it's regenerated in full
every time the script runs (including on a `--dry-run`-free resume), so
it's always consistent with whatever injection files currently exist.

**Resume semantics**: on start, `run-campaign.py` reads
`injection-1.json, injection-2.json, ...` in order until it hits a missing
one, and reconstructs `history` (candidate + weakness signals) from what it
finds. Both implemented strategies resume correctly from this: `random`'s
ordering depends only on the seed and injection index, not on outcomes;
`coverage`'s ordering depends only on which `(category, service)` pairs
have already been tried, which is recoverable from `history` regardless of
whether those prior injections errored out. A campaign interrupted mid-way
(Ctrl-C, cluster blip) is safe to re-invoke with the same
`--tool/--strategy/--seed/--campaign` and will continue from the first
missing injection.

---

## 4. Raw-timeseries sidecar (all three modes)

Every run in every mode -- `benchmark`, `overhead`, and each campaign
injection -- writes a gzipped sidecar next to its result JSON, e.g.:

```
data/chaos-mesh/p1/run-3.json
data/chaos-mesh/p1/run-3.timeseries.json.gz
data/campaigns/random/campaign-1/injection-4.json
data/campaigns/random/campaign-1/injection-4.timeseries.json.gz
```

This exists to feed a later ML anomaly-detection pass over per-run
telemetry, which needs raw timeseries rather than the per-phase aggregates
already in the run JSON.

**Window**: baseline-phase start minus 60s, through cooldown end (or
recovery end, for campaign injections which have no cooldown phase, and
load-window end for overhead `baseline`/`idle` configs which have no
fault at all).

**Resolution**: 5s step range queries (vs. 15s for the existing per-phase
`infra_metrics` in the run JSON, which is unchanged).

**Metrics captured** (`chaoslib.TIMESERIES_QUERIES`): per-pod container CPU
and memory working set, per-pod network rx/tx bytes, pod restart counts
(all widened from the existing per-phase `INFRA_QUERIES` to full-run range
queries), plus node-level CPU utilization and memory used (from
kube-prometheus-stack's node-exporter), plus best-effort per-service HTTP
request rate / error rate / latency histogram buckets. The HTTP metrics use
Envoy/Istio-style metric names as a guess; DeathStarBench's stock
nginx-thrift + Thrift services do not export L7 metrics unless the cluster
has a service mesh installed, so an empty result for those three is
expected on an un-meshed cluster, not a bug.

**Sidecar schema**:

```json
{
  "metadata": {
    "window_start": 1234567890.0,
    "window_end": 1234568500.0,
    "fault_start": 1234568000.0,
    "fault_end": 1234568120.0,
    "step": "5s",
    "namespace": "social-network"
  },
  "metrics": {
    "container_cpu_usage": {"query": "...", "result": [...Prometheus matrix result...]},
    "container_memory_working_set": {"query": "...", "result": [...]},
    "...": "..."
  }
}
```

`fault_start`/`fault_end` are `null` for overhead `baseline`/`idle` configs
(no fault injected). These two fields are the ML labels: everything inside
`[fault_start, fault_end]` is a positive window, everything outside is
negative.

**Failure tolerance**: a failing or empty Prometheus query is recorded with
an `"error"` key (or an empty `"result"` list) rather than aborting
collection; if Prometheus is unreachable for the whole run, the sidecar is
skipped entirely (logged as a warning) and the run's own JSON is written
and the run is reported successful regardless -- the sidecar never causes a
run to fail. See `chaoslib.collect_run_timeseries` and
`chaoslib.write_timeseries_sidecar`.
