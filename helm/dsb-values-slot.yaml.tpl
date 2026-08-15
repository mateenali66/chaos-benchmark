# Per-slot nodeSelector overlay for slot parallelism.
#
# Rendered by scripts/deploy-dsb.sh (${CHAOS_SLOT} substituted with the
# actual slot number, "1" or "2") into a temp file, then passed as a SECOND
# --values flag to `helm upgrade --install`, on top of the existing
# helm/dsb-values.yaml -- so it only overrides the nodeSelector key; the
# resource limits/requests in dsb-values.yaml are untouched. Never used for
# slot 0/unset (deploy-dsb.sh's default path passes only dsb-values.yaml,
# exactly as before slot parallelism existed).
#
# Node-label provisioning (label key "chaos-slot", values "0"/"1"/"2" on 3
# dedicated nodes per slot per cluster) is done separately by whoever has
# cluster access; this file only assumes the label already exists on the
# target nodes by the time this chart is installed. See
# scripts/SLOT_PARALLELISM.md.
#
# IMPORTANT / KNOWN GAP: as of this revision, the vendored DeathStarBench
# chart (DeathStarBench/socialNetwork/helm-chart/socialnetwork/templates/
# _baseDeployment.tpl and _baseNginxDeployment.tpl) does NOT read any
# nodeSelector key from values at all -- grep of templates/ for
# "nodeSelector" returns zero hits. Setting global.nodeSelector here is
# necessary but NOT sufficient on its own to actually pin pods to a slot's
# nodes; it is a no-op against the chart as currently vendored. Whoever
# wires up live nodeSelector/RBAC provisioning needs to also either:
#   (a) patch _baseDeployment.tpl and _baseNginxDeployment.tpl to add a
#       nodeSelector block following the same
#       `hasKey .Values "X" / hasKey $.Values.global "X"` pattern already
#       used there for `resources` and `topologySpreadConstraints`, or
#   (b) apply nodeSelector out-of-band (e.g. a kustomize/kubectl patch after
#       `helm template`, or a namespace-scoped scheduling admission policy).
# This file is still created and wired up as instructed so the values
# contract exists and deploy-dsb.sh's --values plumbing is ready; only the
# chart-side consumption of the key is the open item.
global:
  nodeSelector:
    chaos-slot: "${CHAOS_SLOT}"
