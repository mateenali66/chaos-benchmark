################################################################################
# Litmus RBAC for a slot namespace (slot parallelism).
#
# The litmus-admin ClusterRole (manifests/litmus-rbac.yaml) is cluster-scoped
# and already usable from any namespace; only the ServiceAccount and its
# binding need to exist per-slot. This template adds a slot-specific SA plus
# a uniquely-named ClusterRoleBinding to the SAME shared ClusterRole, so the
# ClusterRole itself is declared exactly once regardless of slot count.
#
# Rendered by post-deploy.sh for CHAOS_SLOT != 0 (slot 0 uses the static
# litmus-rbac.yaml applied once, unchanged).
################################################################################

apiVersion: v1
kind: ServiceAccount
metadata:
  name: litmus-admin
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/part-of: chaos-benchmark
    app.kubernetes.io/component: litmus-rbac
    chaos-slot: "${CHAOS_SLOT}"

---

apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: litmus-admin-slot-${CHAOS_SLOT}
  labels:
    app.kubernetes.io/part-of: chaos-benchmark
    app.kubernetes.io/component: litmus-rbac
    chaos-slot: "${CHAOS_SLOT}"
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: litmus-admin
subjects:
  - kind: ServiceAccount
    name: litmus-admin
    namespace: ${NAMESPACE}
