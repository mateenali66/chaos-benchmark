#!/usr/bin/env bash
################################################################################
# Per-run application state reset.
#
# Pilot data (Aug 15) showed monotonic degradation across repetitions: each
# 300s load window writes ~24k posts into MongoDB/Redis with no cleanup, so
# by rep 10 the app served 11 rps at 74% errors vs 160 rps at 1.5% fresh.
# Repetitions are only i.i.d. if each starts from identical state.
#
# Mechanism: delete all stateful pods (mongodb/redis/memcached use emptyDir,
# so pod recreation = clean store), wait for readiness, re-initialize the
# social graph, verify a timeline read returns data.
#
# Usage: reset-app-state.sh   (kubeconfig context = target cluster, via
#        KUBECONFIG env as in the campaign runners)
#
# Slot parallelism: CHAOS_SLOT env var (default "0") selects which of the
# cluster's up-to-3 isolated namespaces to reset. Unset/"0" is the original
# behavior, byte-for-byte -- namespace "social-network", no port change
# beyond the pre-existing KUBECONFIG-checksum offset. See
# scripts/SLOT_PARALLELISM.md.
################################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Exported so the init-social-graph.sh call below (and any other child
# process) sees the same slot regardless of whether the caller exported it.
export CHAOS_SLOT="${CHAOS_SLOT:-0}"

# Namespace convention for slot parallelism (slot 0/unset = default
# namespace; slot N = "social-network-N"). Kept in sync with
# chaoslib.namespace_for_slot() and init-social-graph.sh -- if you change
# this logic, change it in all three places.
if [[ -z "${CHAOS_SLOT}" || "${CHAOS_SLOT}" == "0" ]]; then
    NAMESPACE="social-network"
else
    NAMESPACE="social-network-${CHAOS_SLOT}"
fi

echo "[reset] deleting stateful pods..."
kubectl delete pods -n "$NAMESPACE" \
  -l 'app in (post-storage-mongodb,user-timeline-mongodb,social-graph-mongodb,media-mongodb,url-shorten-mongodb,user-mongodb,user-memcached,home-timeline-redis,social-graph-redis,user-timeline-redis,post-storage-memcached,media-memcached,url-shorten-memcached,compose-post-redis)' \
  --wait=true 2>/dev/null || true

echo "[reset] waiting for pods to return..."
kubectl wait --for=condition=Ready pods --all -n "$NAMESPACE" --timeout=300s > /dev/null

echo "[reset] re-initializing social graph..."
"$SCRIPT_DIR/init-social-graph.sh" > /dev/null

echo "[reset] verifying timeline read..."
# Slot-specific offset (kept in sync with chaoslib.slot_port_offset() /
# chaoslib.SLOT_PORT_STEP, same 97-per-slot step) stacked on top of the
# existing KUBECONFIG-checksum offset, so 3 slots sharing one KUBECONFIG
# never collide on this verification port-forward. Slot 0/unset adds 0
# (unchanged behavior).
SLOT_PORT_OFFSET=0
if [[ -n "${CHAOS_SLOT}" && "${CHAOS_SLOT}" != "0" ]]; then
    SLOT_PORT_OFFSET=$(( CHAOS_SLOT * 97 ))
fi
VERIFY_PORT=$(( 29080 + $(printf '%s' "${KUBECONFIG:-default}" | cksum | cut -d' ' -f1) % 500 + SLOT_PORT_OFFSET ))
kubectl port-forward svc/nginx-thrift "${VERIFY_PORT}:8080" -n "$NAMESPACE" &>/dev/null &
PF=$!
sleep 3
RESP=$(curl -sf "http://localhost:${VERIFY_PORT}/wrk2-api/user-timeline/read?user_id=1&start=0&stop=5" 2>/dev/null || echo "")
kill "$PF" 2>/dev/null || true
if [ -z "$RESP" ]; then
    echo "[reset] ERROR: timeline read empty after reset" >&2
    exit 1
fi
echo "[reset] done"
