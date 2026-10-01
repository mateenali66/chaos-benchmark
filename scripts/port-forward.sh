#!/usr/bin/env bash
set -euo pipefail

################################################################################
# Forward the dashboard ports to localhost: Grafana 3000, Prometheus 9090,
# Jaeger 16686, Chaos Mesh 2333, LitmusChaos 9091. A forward to a service that
# is not installed on the cluster fails without stopping the others.
################################################################################

echo "=== Starting port forwards ==="
echo "Press Ctrl+C to stop all"
echo ""

echo "Grafana:        http://localhost:3000  (admin / chaos-bench-2026)"
kubectl -n monitoring port-forward svc/prometheus-grafana 3000:80 &

echo "Prometheus:     http://localhost:9090"
kubectl -n monitoring port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090 &

echo "Jaeger:         http://localhost:16686"
kubectl -n monitoring port-forward svc/jaeger 16686:16686 &

echo "Chaos Mesh:     http://localhost:2333"
kubectl -n chaos-testing port-forward svc/chaos-dashboard 2333:2333 &

echo "LitmusChaos:    http://localhost:9091"
kubectl -n litmus port-forward svc/litmusportal-frontend-service 9091:9091 &

echo ""
echo "All port forwards running in background."
echo "Use 'kill %1 %2 %3 %4 %5' or Ctrl+C to stop."
echo ""

# Wait for all background jobs
wait
