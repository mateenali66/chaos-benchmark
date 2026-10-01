# Slot parallelism

A cluster can run up to three isolated copies of the DeathStarBench application at once, one per namespace ("slot"). Each slot runs its own subset of scenarios or campaigns with the same protocol. With `CHAOS_SLOT` unset or `0`, every script uses the single namespace `social-network`.

| Slot | Namespace | Node label |
|---|---|---|
| 0 (default) | `social-network` | `chaos-slot=0` |
| 1 | `social-network-1` | `chaos-slot=1` |
| 2 | `social-network-2` | `chaos-slot=2` |

The mapping is implemented in `chaoslib.namespace_for_slot()` and repeated in `reset-app-state.sh`, `init-social-graph.sh`, `deploy-dsb.sh` and `post-deploy.sh`. Keep them in sync.

## Nodes

Label the nodes for each slot by hand (`kubectl label node <node> chaos-slot=<slot>`). No script applies labels. The vendored chart does not read a `nodeSelector` value, so `helm/dsb-values-slot.yaml.tpl` has no effect. Instead, `deploy-dsb.sh` patches every Deployment in slot 1 or 2 with `nodeSelector: {chaos-slot: <slot>}` after `helm install` and waits for the rollout. Slot 0 is not pinned.

As run, `bench-a` and `bench-b` had 9 m5.xlarge nodes during the slot-parallel Component 1 runs, and `is-chaos-ml` had 3 m5.2xlarge nodes for the Component 3 campaigns.

## Deploy each slot

```bash
./scripts/deploy-dsb.sh                  # slot 0
./scripts/deploy-dsb.sh 1                # slot 1 (creates the namespace)
CHAOS_SLOT=2 ./scripts/deploy-dsb.sh     # slot 2 (a positional argument overrides CHAOS_SLOT)

./scripts/post-deploy.sh                 # slot 0
CHAOS_SLOT=1 ./scripts/post-deploy.sh
CHAOS_SLOT=2 ./scripts/post-deploy.sh
```

Litmus RBAC: slot 0 applies `manifests/litmus-rbac.yaml`, which also creates the shared `litmus-admin` ClusterRole. Slots 1 and 2 render `manifests/litmus-rbac-slot.yaml.tpl`, which adds a ServiceAccount in the slot namespace and its own ClusterRoleBinding to that ClusterRole. Apply slot 0 first.

## Run

Component 1, one process per slot, with disjoint scenario sets and the same `CHAOS_DATA_DIR`:

```bash
./scripts/run-all-experiments.sh --tool chaos-mesh --scenarios p1,p2,p3,n1 --reps 15
./scripts/run-all-experiments.sh --tool chaos-mesh --scenarios n2,n3,n4,n5 --slot 1 --reps 15
./scripts/run-all-experiments.sh --tool chaos-mesh --scenarios r1,r2,a1,a2 --slot 2 --reps 15
```

This is the split used for Component 1. `--slot N` exports `CHAOS_SLOT=N` for `run-experiment.py` and `reset-app-state.sh` and prefixes each progress-log line with `[slot N]`. Output paths are `${CHAOS_DATA_DIR}/{tool}/{scenario}/run-N.json` with no slot component, so two slots given the same scenario and tool would write the same files. The scripts do not check that the sets are disjoint.

Component 3, one process per slot (the driver assigns campaigns to slots itself):

```bash
CHAOS_SLOT=0 ./scripts/run-all-campaigns.sh
CHAOS_SLOT=1 ./scripts/run-all-campaigns.sh
CHAOS_SLOT=2 ./scripts/run-all-campaigns.sh
```

## Ports and job names

- Local port-forwards add `slot * 97` (`chaoslib.SLOT_PORT_STEP`) to their base port so slots on one cluster do not collide. `reset-app-state.sh` and `init-social-graph.sh` also add a per-cluster offset derived from a checksum of `KUBECONFIG`. `post-deploy.sh` uses base port 18080.
- All slots query the one Prometheus in the `monitoring` namespace through the same port (`CHAOS_PROM_PORT`). Queries select the slot by namespace.
- wrk2 Jobs run in the slot namespace and target `nginx-thrift.<namespace>:8080`. Job names are `wrk2-{label}-run{run}` for slot 0 and `wrk2-{slot}-{label}-run{run}` for slots 1 and 2 (`chaoslib.wrk2_job_name`).

## Timeseries sidecars

`chaoslib.write_timeseries_sidecar` takes a `namespace` argument that defaults to `social-network`, and its callers in `run-experiment.py` and `run-campaign.py` do not pass the slot namespace. Sidecars written by slots 1 and 2 therefore hold the container and pod series of the `social-network` namespace (slot 0), and `metadata.namespace` records `social-network`. Node-level series cover the whole cluster. The per-phase `infra_metrics` in the run JSON files do use the slot namespace.
