# Per-slot values overlay for slots 1 and 2 (scripts/SLOT_PARALLELISM.md).
#
# scripts/deploy-dsb.sh substitutes ${CHAOS_SLOT} and passes this file as a
# second --values file after helm/dsb-values.yaml. The vendored chart does not
# read nodeSelector, so this overlay has no effect. deploy-dsb.sh pins slot
# pods by patching each Deployment's nodeSelector after install.
global:
  nodeSelector:
    chaos-slot: "${CHAOS_SLOT}"
