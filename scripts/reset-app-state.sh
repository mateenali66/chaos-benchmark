#!/usr/bin/env bash
################################################################################
# Reset the application state before a repetition or campaign injection.
#
# Deletes the stateful pods (MongoDB, Redis and Memcached use emptyDir, so a
# recreated pod starts with an empty store), waits for all pods to be Ready,
# re-initializes the social graph and checks that a timeline read returns
# data. Without a reset, writes accumulate across repetitions and the
# application slows down, so repetitions would not start from the same state
# (analysis/PREREGISTRATION.md, Component 1 amendment item 3).
# run-all-experiments.sh, run-overhead.sh and run-campaign.py call it.
#
# Env: KUBECONFIG selects the cluster. CHAOS_SLOT (default 0) selects the
# namespace (see scripts/SLOT_PARALLELISM.md).
# Usage: ./scripts/reset-app-state.sh
################################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Exported so init-social-graph.sh uses the same slot.
export CHAOS_SLOT="${CHAOS_SLOT:-0}"

# Namespace for this slot. Keep in sync with chaoslib.namespace_for_slot() and
# init-social-graph.sh.
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
# Port 29080 plus an offset from a checksum of KUBECONFIG plus 97 per slot
# (chaoslib.SLOT_PORT_STEP), so resets on different clusters or slots do not
# collide on this port-forward.
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
