#!/usr/bin/env python3
"""Practitioner-heuristic baseline for Component 4 (analysis/PREREGISTRATION.md).

The rules were frozen on 2026-08-16, after Component 1 had finished and
before any Component 4 generation or Component 3 injection. They were
written from fault-type mechanics (how a pod kill, network delay, resource
stress or application-layer fault usually behaves under load) and the
steady-state baseline figures only (throughput and latency with no fault
active). No Component 1 fault-window result was read. The plan asked for
rules frozen before any data collection, which this does not meet (see the
erratum in PREREGISTRATION.md). The git history records the freeze.

Deterministic: the same candidate always gets the same prediction. The calls
are coarse, the kind an on-call engineer would make from the fault type and
target alone, and serve as a floor for the LLM predictions.
"""

from __future__ import annotations


def predict(candidate: dict) -> dict:
    """Predict the outcome of one fault-space candidate.

    candidate: one entry from experiments/fault-space.yaml (scenario_template,
    target_service, param). Returns the same shape as the LLM's hypothesis
    output: {throughput_direction, degraded_services, weakness_signal_present,
    rationale}. Raises ValueError for an unknown scenario_template."""
    t = candidate["scenario_template"]
    svc = candidate["target_service"]

    # Pod-level disruption: one pod is killed, fails, or has a container
    # killed. Each service has one replica in the DSB chart, so the pod is
    # unavailable until Kubernetes reschedules it, and pod_restarts > 0 is
    # guaranteed, which alone fires the weakness signal. Requests to that
    # service fail or queue until the reschedule, which is usually quick at
    # 120 rps, so throughput dips rather than collapses. Call it degrade:
    # the 60 s recovery phase is sized to capture such a dip.
    if t in ("pod-kill", "pod-failure", "container-kill"):
        return {
            "throughput_direction": "degrade",
            "degraded_services": [svc],
            "weakness_signal_present": True,
            "rationale": "Single-replica pod disruption guarantees a pod "
                         "restart (weakness signal by definition) and a "
                         "transient availability gap on the target service "
                         "while Kubernetes reschedules.",
        }

    # Network faults: latency, loss or partition on one service compounds
    # through any downstream call chain, and severity scales with the
    # injected magnitude. 50 ms is close to normal tail latency and likely
    # absorbed. 100 ms, 300 ms, packet loss and partition are large next to
    # the app's baseline range (p50 ~12 ms, p99 ~83 ms) and should visibly
    # degrade it.
    if t == "latency-50ms":
        return {
            "throughput_direction": "no_meaningful_change",
            "degraded_services": [],
            "weakness_signal_present": False,
            "rationale": "50ms is within the app's normal p50-p99 baseline "
                         "spread and likely absorbed without a visible "
                         "aggregate effect.",
        }
    if t in ("latency-100ms", "latency-300ms", "packet-loss-5pct", "network-partition"):
        return {
            "throughput_direction": "degrade",
            "degraded_services": [svc],
            "weakness_signal_present": True,
            "rationale": "Injected delay/loss/partition on this magnitude "
                         "is large relative to the app's baseline latency "
                         "and directly slows or blocks requests touching "
                         "the target service.",
        }

    # Resource stress: CPU or memory pressure at 80% cuts the target's own
    # processing capacity, enough to expect a visible throughput impact.
    if t in ("cpu-stress-80pct", "memory-pressure-80pct"):
        return {
            "throughput_direction": "degrade",
            "degraded_services": [svc],
            "weakness_signal_present": True,
            "rationale": "80% resource stress directly contends with the "
                         "target service's own request-handling capacity.",
        }

    # Application-layer faults: forced 503 or gRPC UNAVAILABLE responses
    # produce errors by construction. Any meaningful share of requests that
    # reach the target during the fault fails, which alone crosses the 5%
    # error-rate threshold at this app's request mix.
    if t in ("http-abort-503", "grpc-unavailable"):
        return {
            "throughput_direction": "degrade",
            "degraded_services": [svc],
            "weakness_signal_present": True,
            "rationale": "Forced error responses from the target directly "
                         "inflate the error rate past the 5% weakness "
                         "threshold and remove successful throughput from "
                         "that service's share of the request mix.",
        }

    raise ValueError(f"practitioner_heuristic.predict(): unknown scenario_template {t!r}")
