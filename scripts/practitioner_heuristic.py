#!/usr/bin/env python3
"""Component 4's practitioner-heuristic baseline (analysis/PREREGISTRATION.md:
"a practitioner-heuristic prediction (rules written and frozen before data
collection) and chance").

Timing disclosure (frozen 2026-08-16, same day as this component's real-mode
wiring, after Component 1 data collection had already finished): the
pre-registration's original intent was rules frozen before ANY data
collection, which is no longer literally achievable since Component 1 is
done. What IS preserved: these rules were written from general fault-type
mechanics (a practitioner's domain reasoning about how a pod-kill, a network
delay, a resource-stress fault, or an application-layer fault typically
behaves under load) WITHOUT reading any Component 1 fault-window result --
only the steady-state baseline numbers (throughput/latency at rest, no fault
active) were consulted, which is not the outcome being predicted. This is
the same commit that adds this file; the git history is the record of
"before generation began," not "before Component 1 ran."

Deterministic: same candidate always produces the same prediction (no LLM
call, no randomness). Not scored against per-run noise -- these are the
kind of confident, coarse-grained calls an experienced on-call engineer
would make from the fault type and target alone, which is exactly what the
comparison to Component 4's LLM predictions is meant to establish a floor
against.
"""

from __future__ import annotations


def predict(candidate: dict) -> dict:
    """candidate: one entry from experiments/fault-space.yaml (scenario_template,
    target_service, param). Returns the same shape as the LLM's hypothesis
    output: {throughput_direction, degraded_services, weakness_signal_present,
    rationale}."""
    t = candidate["scenario_template"]
    svc = candidate["target_service"]

    # Pod-level disruption: a single pod is killed/fails/has one container
    # killed. At this app's replica count (1 per service in the DSB chart),
    # the pod is unavailable until Kubernetes reschedules it -- pod_restarts
    # > 0 is definitionally guaranteed for this fault family, which alone
    # satisfies the weakness-signal trigger regardless of anything else.
    # Throughput impact is a coarse call: with only one replica, in-flight
    # requests to that service fail/queue until reschedule completes, but
    # the reschedule is typically fast enough at this load (120 rps) that
    # aggregate throughput dips rather than collapses -- call it degrade,
    # since the RECOVERY protocol phase (60s) is specifically sized to
    # capture exactly this kind of transient dip.
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

    # Network faults: injected latency/loss/partition on one service
    # compounds through any downstream call chain. Severity scales with the
    # injected magnitude -- 50ms is close to normal tail latency and likely
    # absorbed, 100ms/300ms and packet loss/partition are large enough
    # relative to this app's ~12-83ms baseline range to visibly degrade.
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

    # Resource stress: CPU/memory pressure on the target degrades its own
    # processing capacity directly (that IS the fault), and the fault's own
    # weight (80%) is high enough to expect visible throughput impact.
    if t in ("cpu-stress-80pct", "memory-pressure-80pct"):
        return {
            "throughput_direction": "degrade",
            "degraded_services": [svc],
            "weakness_signal_present": True,
            "rationale": "80% resource stress directly contends with the "
                         "target service's own request-handling capacity.",
        }

    # Application-layer faults: forced error responses (503 / gRPC
    # UNAVAILABLE) ARE the error-rate signal by construction -- any
    # meaningful fraction of requests hitting the target during the fault
    # window will register as failed, which alone crosses the 5%
    # error-rate weakness threshold at this app's request mix.
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
