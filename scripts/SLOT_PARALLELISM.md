# Slot Parallelism

Execution-concurrency extension: each cluster can run up to 3 concurrent,
fully isolated copies of the DeathStarBench stack, one per Kubernetes
namespace, each pinned to its own 3 dedicated nodes. This lets 3 scenario
groups run at once per cluster instead of serially, **without changing the
statistical design** -- same protocol, same tool per cluster per phase, same
reps. It is purely an execution-concurrency change.

**Backward compatibility is the load-bearing constraint here.** Every script
touched by this feature defaults to slot 0 when `CHAOS_SLOT` is unset, and
slot 0 reproduces the original single-namespace behavior byte-for-byte:
namespace `social-network`, no nodeSelector, no overlay files, unchanged Job
names, unchanged log format. A campaign invoked without any of the flags or
env vars described below is unaffected by anything in this document.

---

## 1. Namespace convention

| Slot | Namespace | Node label `chaos-slot` |
|------|-----------|--------------------------|
| 0 (default/unset) | `social-network` | `"0"` |
| 1 | `social-network-1` | `"1"` |
| 2 | `social-network-2` | `"2"` |

Implemented identically in three places (each cross-references the others in
comments -- keep them in sync if this ever changes):

- `scripts/chaoslib.py`: `namespace_for_slot(slot)`
- `scripts/reset-app-state.sh`
- `scripts/init-social-graph.sh`

`scripts/deploy-dsb.sh` and `scripts/post-deploy.sh` re-derive the same
mapping inline (bash, not sourced from a shared file, so they too carry a
cross-reference comment back to `chaoslib.namespace_for_slot()`).

---

## 2. Node labeling (ops responsibility, outside this script layer)

Before deploying into slot 1 or 2, whoever has cluster access must:

1. Label 3 dedicated nodes per slot per cluster:
   ```
   kubectl label node <node-name> chaos-slot=0
   kubectl label node <node-name> chaos-slot=1
   kubectl label node <node-name> chaos-slot=2
   ```
2. Ensure enough total node capacity for 3 concurrent DeathStarBench copies
   (each copy currently assumes ~3x m5.xlarge-equivalent capacity per
   `helm/dsb-values.yaml`'s sizing comment -- 3 slots means roughly 3x that).

None of the scripts in this repo apply node labels themselves. See the
**Known gap** below: the node label alone is not sufficient either, because
the vendored chart doesn't yet consume a nodeSelector value.

---

## 3. How to invoke a slot

### Deploy

```bash
./scripts/deploy-dsb.sh              # slot 0 (default): namespace social-network
./scripts/deploy-dsb.sh 1            # slot 1: namespace social-network-1, creates it if missing
CHAOS_SLOT=2 ./scripts/deploy-dsb.sh # slot 2 via env var (positional arg wins if both given)
```

### Post-deploy (RBAC / ChaosExperiment CRDs / social graph init)

```bash
./scripts/post-deploy.sh             # slot 0 (default)
CHAOS_SLOT=1 ./scripts/post-deploy.sh
CHAOS_SLOT=2 ./scripts/post-deploy.sh
```

### Run a batch of experiments

```bash
# slot 0 (default), all 12 scenarios -- unchanged from before slot support
./scripts/run-all-experiments.sh --tool chaos-mesh --reps 30

# slot 1, a scenario subset
./scripts/run-all-experiments.sh --tool chaos-mesh --scenarios n2,n3,n4,n5 --slot 1 --reps 30

# slot 2, a scenario subset, via env var instead of --slot
CHAOS_SLOT=2 ./scripts/run-all-experiments.sh --tool chaos-mesh --scenarios r1,r2,a1,a2 --reps 30
```

`--slot`/`CHAOS_SLOT` is exported for the duration of the invocation, so it
reaches `run-experiment.py` (via `chaoslib`'s env-driven defaults) and
`reset-app-state.sh` automatically -- no separate plumbing needed.

### Env var / flag contract summary

| Mechanism | Where | Slot 0 / unset | Slot 1 or 2 |
|-----------|-------|-----------------|--------------|
| `CHAOS_SLOT` env var | `chaoslib.py`, all touched bash scripts | `"0"` (default) -> original namespace/ports/names | selects namespace, port offsets, Job-name prefix |
| `deploy-dsb.sh` positional arg / `CHAOS_SLOT` | `deploy-dsb.sh` | no overlay, no namespace creation | creates namespace if missing, renders + applies `helm/dsb-values-slot.yaml.tpl` |
| `--slot N` | `run-all-experiments.sh` | no log prefix | exports `CHAOS_SLOT=N`, prefixes progress-log lines with `[slot N] ` |
| `--scenarios p1,p2,...` | `run-all-experiments.sh` | omitted -> all 12 scenarios (unchanged) | restricts this invocation to the given scenario IDs |

---

## 4. Caller responsibility: disjoint scenario sets

`run-all-experiments.sh`'s output path is
`${CHAOS_DATA_DIR}/{tool}/{scenario}/run-{rep}.json` -- it does **not**
include the slot. This is intentional: a run's `(tool, scenario, rep)` triple
is assumed to already be globally unique, and `CHAOS_DATA_DIR` should
normally be the **same shared directory** across a cluster's concurrent slot
invocations (not a separate directory per slot), so that resume/skip
semantics still work correctly across the whole campaign.

This is only safe if the `--scenarios` sets passed to concurrent slots on
one cluster **never overlap**. If two slots were given the same scenario for
the same tool, they would race on the same output file. Enforcing disjoint
scenario sets is the caller's responsibility -- the scripts do not check it.

### Worked example: 3-way split of the 12 scenarios

```bash
# slot 0 -- p1, p2, p3, n1
./scripts/run-all-experiments.sh --tool chaos-mesh --scenarios p1,p2,p3,n1 --reps 30

# slot 1 -- n2, n3, n4, n5
./scripts/run-all-experiments.sh --tool chaos-mesh --scenarios n2,n3,n4,n5 --slot 1 --reps 30

# slot 2 -- r1, r2, a1, a2
./scripts/run-all-experiments.sh --tool chaos-mesh --scenarios r1,r2,a1,a2 --slot 2 --reps 30
```

Run all 3 concurrently on one cluster (3 separate shells/processes) and the
12-scenario x 30-rep sweep for one tool completes in roughly a third of the
wall-clock time of the serial version, with the same statistical design.

---

## 5. Port-collision avoidance

Local verification/init port-forwards derive a base offset from a checksum
of `KUBECONFIG` (separates *clusters*, mod 500). That alone does nothing for
3 slots sharing one `KUBECONFIG` on the same cluster, which is the new case
slot parallelism introduces. A second, smaller deterministic offset --
`slot * 97` (`chaoslib.SLOT_PORT_STEP`) -- is stacked on top, so the 3 slots'
port-forwards never collide with each other:

- `scripts/chaoslib.py`: `slot_port_offset(slot, base)` (pure helper,
  documents the convention for any future Python caller)
- `scripts/reset-app-state.sh`: verification port-forward
- `scripts/init-social-graph.sh`: init/verify port-forward
- `scripts/post-deploy.sh`: social-graph verification port-forward

`PROMETHEUS_PORT` is **not** slot-offset -- all 3 slots query the same
shared Prometheus instance in the `monitoring` namespace, distinguished by
the `namespace=` argument passed to the query functions, not by port.

wrk2 Jobs don't need a port offset at all: they run as in-cluster
`batch/v1` Jobs hitting `nginx-thrift.<namespace>` directly, not via a local
port-forward. They're kept collision-free by not sharing a namespace
(different Job namespace per slot) and by a Job-name prefix (see below).

---

## 6. Job-name collision avoidance

`chaoslib.wrk2_job_name(label, run, slot=None)` (slot defaults to
`CHAOS_SLOT`) keeps the original `wrk2-{label}-run{run}` pattern for slot
0/unset, and prefixes the slot for any other slot:
`wrk2-{slot}-{label}-run{run}`. This matters because two slots running the
same scenario label concurrently (e.g. both mid-warmup) would otherwise
produce identical Job names even though they're going into different
namespaces -- harmless for `kubectl apply` (different namespace = different
object) but confusing in `kubectl get jobs -A` output and log greps, so the
prefix is there for legibility as much as correctness.

---

## 7. Known gap: nodeSelector is not yet consumed by the vendored chart

`helm/dsb-values-slot.yaml.tpl` sets `global.nodeSelector.chaos-slot`, and
`deploy-dsb.sh` renders and passes it as a second `--values` file on top of
`helm/dsb-values.yaml`. **This alone does not pin pods to a slot's nodes.**
As of this revision, `DeathStarBench/socialNetwork/helm-chart/socialnetwork/templates/_baseDeployment.tpl`
and `_baseNginxDeployment.tpl` (the two templates every service in the chart
is built from) never read any `nodeSelector` key -- `grep -r nodeSelector
templates/` in the chart returns zero hits. The values overlay is wired up
and ready, but is currently a no-op against the chart as vendored.

Whoever wires up the live nodeSelector/RBAC provisioning needs to do one of:

- **(a)** Patch `_baseDeployment.tpl` and `_baseNginxDeployment.tpl` to add
  a `nodeSelector` block, following the exact pattern the chart already uses
  for `resources` and `topologySpreadConstraints`:
  ```
  {{- if hasKey .Values "nodeSelector" }}
        nodeSelector:
          {{ toYaml .Values.nodeSelector | nindent 8 | trim }}
        {{- else if hasKey $.Values.global "nodeSelector" }}
        nodeSelector:
          {{ toYaml $.Values.global.nodeSelector | nindent 8 | trim }}
        {{- end }}
  ```
- **(b)** Apply the nodeSelector out-of-band after `helm template`/`helm
  install` (a kustomize patch, a `kubectl patch` pass, or a namespace-scoped
  scheduling admission policy that maps namespace -> node label).

## 8. Other open items for cluster-side provisioning

- **RBAC**: `manifests/litmus-rbac.yaml` (applied unconditionally by
  `post-deploy.sh` step 1) hardcodes `namespace: social-network` for the
  `litmus-admin` ServiceAccount and the `ClusterRoleBinding` subject. It was
  intentionally left unmodified for this revision (editing it wasn't in
  scope and doing so without cluster access to verify is risky), but running
  `post-deploy.sh` with `CHAOS_SLOT=1` or `2` today will still only create
  the SA in `social-network`, not in the slot's namespace -- ChaosEngine
  execution in `social-network-1`/`social-network-2` has no ServiceAccount
  until this is addressed. Options: parameterize the manifest per slot with
  slot-specific SA/binding names (avoids clobbering when re-applied), or
  extend the single ClusterRoleBinding's `subjects` list to include all 3
  slots' ServiceAccounts unconditionally.
- **Capacity**: confirm 3x the current per-slot node capacity is actually
  available/budgeted before running all 3 slots concurrently; nothing in
  this script layer checks or enforces that.
- **Litmus ChaosExperiment CRDs are cluster-scoped, not namespaced** in the
  underlying `litmuschaos.io` API despite `post-deploy.sh` passing `-n
  ${NAMESPACE}` to `kubectl apply` -- worth double-checking on a live
  cluster that installing them once (slot 0) doesn't already cover slots 1
  and 2, which would make step 3 a harmless no-op for those slots rather
  than a real per-slot install. Not verified against a live cluster as part
  of this change (no cluster access was used).
