#!/usr/bin/env python3
"""
Chaos Benchmark Campaign Runner (ML fault-selection study scaffold)

A campaign = a sequence of K=10 fault injections, chosen one at a time by a
pluggable strategy from the 36-candidate fault space in
experiments/fault-space.yaml, run back-to-back on a single tool/cluster with
a shortened protocol (BASELINE 120s / FAULT 120s / RECOVERY 60s, no
cooldown) to keep a 10-injection campaign well under two hours.

Reuses chaoslib.run_fault_protocol for the actual injection execution (the
same function --mode benchmark and --mode overhead's "fault" config use),
so campaign runs share every kubectl/wrk2/Prometheus code path with the
main benchmark instead of re-implementing it.

Strategies implemented now: random (seeded), coverage (deterministic
round-robin over category x service), and one LLMStrategy instance per
frozen Bedrock arm in experiments/llm-config.yaml (llm-claude, llm-llama,
llm-mistral). See LLMStrategy's docstring for the selection prompt,
validation, and fallback semantics.

Usage:
  python3 run-campaign.py --tool chaos-mesh --strategy random --seed 42 --campaign 1
  python3 run-campaign.py --tool litmus --strategy coverage --campaign 1
  python3 run-campaign.py --tool chaos-mesh --strategy llm-claude --campaign 1
  python3 run-campaign.py --tool chaos-mesh --strategy random --seed 42 --campaign 1 --dry-run

Output:
  data/campaigns/{strategy}/campaign-{N}/injection-{1..10}.json
  data/campaigns/{strategy}/campaign-{N}/injection-{1..10}.timeseries.json.gz
  data/campaigns/{strategy}/campaign-{N}/campaign-summary.json

Resume: if injection-K.json already exists, it is not re-run; its recorded
candidate is folded into `history` before selecting injection K+1 onward, so
`random` (whose ordering depends only on the seed, not on outcomes) and
`coverage` (whose ordering depends only on which (category, service) pairs
have been tried, not on their results) both resume deterministically. The
`campaign-summary.json` is always regenerated from whatever injection files
exist, so it's safe to inspect after a partial/interrupted campaign too.
"""

import argparse
import json
import os
import random
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

import chaoslib
import llmclient

FAULT_SPACE_FILE = chaoslib.EXPERIMENTS_DIR / "fault-space.yaml"
LLM_CONFIG_FILE = chaoslib.EXPERIMENTS_DIR / "llm-config.yaml"
PROMPTS_DIR = chaoslib.PROJECT_ROOT / "scripts" / "prompts"
SELECTION_PROMPT_FILE = PROMPTS_DIR / "selection.txt"
TOPOLOGY_FILE = PROMPTS_DIR / "topology-social-network.md"
CAMPAIGN_K = 10

# Frozen campaign goal text for Component 3 (analysis/PREREGISTRATION.md's
# discovery-curve-AUC primary metric and its weakness-class definition).
# Kept as an f-string on CAMPAIGN_K so the prose can't drift from the actual
# loop bound.
CAMPAIGN_GOAL = (
    f"Maximize the number of DISTINCT resilience weaknesses discovered within "
    f"K={CAMPAIGN_K} fault injections in this campaign. A weakness is a "
    f"(signal, target_service) pair where signal is one of: error_rate > 5%, "
    f"p99 latency > 3x the campaign's running baseline median, recovery_time "
    f"> 60s (the system has not visibly settled by the end of the recovery "
    f"window), or pod_restarts > 0. Repeating a candidate you have already "
    f"tried this campaign cannot surface a new weakness class from it that "
    f"you have not already seen, so prefer untried (category, "
    f"target_service) combinations unless you have a specific reason to "
    f"suspect a prior result was noisy."
)

################################################################################
# Fault space + result types
################################################################################

@dataclass(frozen=True)
class FaultCandidate:
    id: str
    scenario_template: str
    category: str
    target_service: str
    param: dict


@dataclass
class RunResult:
    """One completed (or attempted) injection, as far as a Strategy needs to
    know about it. `weakness_signals` is None for the not-yet-run stub used
    by resume (candidate identity is known, outcome may not be, if only the
    candidate needs reconstructing -- in practice we always have the full
    injection JSON on resume, so this is populated)."""
    candidate: FaultCandidate
    weakness_signals: Optional[dict] = field(default=None)
    error: Optional[str] = None


def load_fault_space(path: Path = FAULT_SPACE_FILE) -> list[FaultCandidate]:
    with open(path) as f:
        doc = yaml.safe_load(f)
    candidates = []
    for entry in doc["candidates"]:
        candidates.append(FaultCandidate(
            id=entry["id"],
            scenario_template=entry["scenario_template"],
            category=entry["category"],
            target_service=entry["target_service"],
            param=entry.get("param") or {},
        ))
    return candidates


def candidate_by_id(fault_space: list[FaultCandidate], candidate_id: str) -> FaultCandidate:
    for c in fault_space:
        if c.id == candidate_id:
            return c
    raise KeyError(f"Candidate id '{candidate_id}' not found in fault space")


_DESCRIPTION_TEMPLATES = {
    "pod-kill": "kills one pod of {svc} (SIGKILL, immediate)",
    "container-kill": "kills the {container} container within a {svc} pod",
    "pod-failure": "forces one pod of {svc} into a failing state for the fault window",
    "latency-50ms": "injects 50ms (+/-10ms jitter) network latency on traffic to {svc}",
    "latency-100ms": "injects 100ms (+/-20ms jitter) network latency on traffic to {svc}",
    "latency-300ms": "injects 300ms (+/-50ms jitter) network latency on traffic to {svc}",
    "packet-loss-5pct": "drops 5% of packets to/from {svc}",
    "network-partition": "partitions {svc} from the rest of the namespace (direction: {direction})",
    "cpu-stress-80pct": "stresses {svc} pods to 80% CPU load ({workers} workers)",
    "memory-pressure-80pct": "applies memory pressure to {svc} pods ({size}MB)",
    "http-abort-503": "aborts HTTP responses from {svc} with a 503 status",
    "grpc-unavailable": "returns gRPC UNAVAILABLE (status 14) from {svc}",
}


def describe_candidate(c: FaultCandidate) -> str:
    """Mechanical, factual one-line description built straight from
    fault-space.yaml's own fields (scenario_template/category/target_service/
    param) -- fed to the LLM strategy's prompt so the model sees id,
    category, target service, and description per the fault-selection design
    (../jss/ML_ARM_DESIGN.md)."""
    template = _DESCRIPTION_TEMPLATES.get(c.scenario_template)
    if template is None:
        return f"{c.scenario_template} fault against {c.target_service}"
    p = c.param
    return template.format(
        svc=c.target_service,
        container=p.get("container", c.target_service),
        direction=p.get("direction", "both"),
        workers=p.get("workers", "?"),
        size=p.get("size_mb", "?"),
    )


def load_llm_config(path: Path = LLM_CONFIG_FILE) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def llm_arm_names(config: Optional[dict] = None) -> list[str]:
    config = config or load_llm_config()
    return [m["arm"] for m in config["models"]]


def llm_model_id(arm: str, config: Optional[dict] = None) -> str:
    config = config or load_llm_config()
    for m in config["models"]:
        if m["arm"] == arm:
            return m["model_id"]
    raise KeyError(f"arm '{arm}' not found in {LLM_CONFIG_FILE}")

################################################################################
# Strategies
################################################################################

class Strategy:
    """Pluggable fault-selection strategy. `select` is called once per
    injection, in order, with the history of injections already completed
    in this campaign (oldest first), the full candidate fault space, and how
    many injections remain including this one. Must return one candidate;
    repeats across a campaign are allowed (K=10 < 36 candidates, so a
    strategy MAY choose not to repeat, but nothing in the interface forbids
    it -- e.g. a future `llm` strategy re-testing a candidate it suspects is
    flaky).
    """

    name = "base"

    def select(self, history: list[RunResult], fault_space: list[FaultCandidate], k_remaining: int) -> FaultCandidate:
        raise NotImplementedError


class RandomStrategy(Strategy):
    """Seeded random sampling without replacement (falls back to
    with-replacement only if k_remaining ever exceeds the fault space size,
    which can't happen at K=10 against 36 candidates, but is handled anyway
    so this stays correct if CAMPAIGN_K ever grows)."""

    name = "random"

    def __init__(self, seed: int):
        self.seed = seed
        self._rng = random.Random(seed)
        self._order: Optional[list[FaultCandidate]] = None

    def select(self, history, fault_space, k_remaining):
        if self._order is None:
            # Deterministic for a given seed + fault_space ordering.
            self._order = list(fault_space)
            self._rng.shuffle(self._order)
        idx = len(history)
        if idx < len(self._order):
            return self._order[idx]
        # Exhausted the space (only reachable if CAMPAIGN_K > len(fault_space));
        # reshuffle and continue with replacement.
        return self._rng.choice(fault_space)


class CoverageStrategy(Strategy):
    """Deterministic round-robin across category x target_service. Rotates
    through categories in a fixed order (pod, network, resource,
    application, as they appear in fault-space.yaml's metadata) and, within
    the category whose turn it is, picks the first not-yet-tried
    (category, service) combination in fault-space.yaml's listed order. Once
    every combination has been tried at least once, falls back to the
    least-tried combination (ties broken by candidate id) so a campaign
    longer than the number of distinct combinations still makes progress
    rather than repeating the same first candidate forever.
    """

    name = "coverage"

    def select(self, history, fault_space, k_remaining):
        categories = sorted({c.category for c in fault_space})
        tried_combos = Counter((r.candidate.category, r.candidate.target_service) for r in history)

        start_idx = len(history) % len(categories)
        for offset in range(len(categories)):
            cat = categories[(start_idx + offset) % len(categories)]
            for c in fault_space:
                if c.category == cat and tried_combos[(c.category, c.target_service)] == 0:
                    return c

        # Every combination tried at least once: pick the globally
        # least-tried combination, deterministic tie-break on candidate id.
        return min(fault_space, key=lambda c: (tried_combos[(c.category, c.target_service)], c.id))


class LLMStrategy(Strategy):
    """LLM-driven fault selection (Component 3, analysis/PREREGISTRATION.md).
    One instance is bound to a single frozen Bedrock arm (llm-claude /
    llm-llama / llm-mistral, experiments/llm-config.yaml). `select()` builds
    a prompt from prompts/selection.txt with the fault space (id, category,
    target_service, description), the topology summary, and this campaign's
    own history so far, asks the model for one JSON object
    {"candidate_id": ..., "rationale": ...}, and validates the answer
    against the fault space AND the not-yet-tried set.

    Validation semantics: an invalid response (malformed JSON, unknown
    candidate_id, or an already-tried candidate_id) is re-prompted --
    independently, not conversation-style, restating the specific error --
    up to MAX_REPROMPTS times. If every attempt is invalid, `select()` falls
    back to a seeded-random pick among untried candidates
    (`self._fallback_rng`, seeded once at construction so a resumed campaign
    reproduces the same fallback choices) and increments
    `self.fallback_count`, which the campaign summary reports as a metric.

    Every request/response -- valid, invalid, or a fallback -- is appended
    as one line to `{campaign_dir}/llm-transcript.jsonl` (model id, prompt,
    raw completion, raw API response, validation outcome). Reproducibility
    depends on this transcript: Bedrock does not expose a sampling seed
    uniformly across the three providers.

    Injectable for tests: pass any object implementing
    `.invoke(prompt, temperature, max_tokens) -> object with .text/.raw`
    (matching llmclient.LLMResponse's shape) as `client`; a real campaign
    run passes an llmclient.BedrockClient bound to this arm's model id.
    """

    MAX_REPROMPTS = 3

    def __init__(self, arm: str, client, campaign_dir: Path, model_id: str,
                 temperature: float = 0.2, max_tokens: int = 1024,
                 fallback_seed: int = 0,
                 prompt_template_path: Path = SELECTION_PROMPT_FILE,
                 topology_path: Path = TOPOLOGY_FILE):
        self.name = arm
        self.arm = arm
        self.client = client
        self.campaign_dir = campaign_dir
        self.model_id = model_id
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.fallback_count = 0
        self._fallback_rng = random.Random(fallback_seed)
        self.prompt_template = Path(prompt_template_path).read_text()
        self.topology_summary = Path(topology_path).read_text()
        self.transcript_path = campaign_dir / "llm-transcript.jsonl"

    def select(self, history: list[RunResult], fault_space: list[FaultCandidate],
               k_remaining: int) -> FaultCandidate:
        tried_ids = {r.candidate.id for r in history}
        not_tried = [c for c in fault_space if c.id not in tried_ids]
        if not not_tried:
            # Every candidate already tried at least once (unreachable at
            # K=10 < 36 candidates, guarded anyway so this stays correct if
            # CAMPAIGN_K ever grows past the fault-space size).
            not_tried = list(fault_space)

        injection_number = len(history) + 1
        previous_error: Optional[str] = None

        for attempt in range(1, self.MAX_REPROMPTS + 1):
            prompt = self._build_prompt(history, fault_space, not_tried,
                                         injection_number, k_remaining, previous_error)
            call = self._call_model(prompt)
            candidate_id, rationale, validation_error = self._validate(
                call["text"], call["error"], fault_space, tried_ids)

            self._log_transcript(
                injection_number=injection_number, attempt=attempt, prompt=prompt,
                raw_completion=call["text"], raw_response=call["raw"],
                model_id=call["model_id"],
                validation_outcome=("valid" if validation_error is None
                                     else f"invalid: {validation_error}"),
                selected_candidate_id=candidate_id, rationale=rationale, fallback=False,
            )
            if validation_error is None:
                return candidate_by_id(fault_space, candidate_id)
            previous_error = validation_error

        # MAX_REPROMPTS invalid attempts: seeded-random fallback, logged and
        # counted so it surfaces as a reported metric in the campaign summary.
        self.fallback_count += 1
        fallback_candidate = self._fallback_rng.choice(not_tried)
        self._log_transcript(
            injection_number=injection_number, attempt=None, prompt=None,
            raw_completion=None, raw_response=None, model_id=self.model_id,
            validation_outcome=f"fallback_after_{self.MAX_REPROMPTS}_invalid_attempts",
            selected_candidate_id=fallback_candidate.id, rationale=None, fallback=True,
        )
        return fallback_candidate

    def _call_model(self, prompt: str) -> dict:
        """Normalizes a successful LLMResponse and a raised exception into
        the same shape, so `_validate` only has one error path to handle
        regardless of whether the model answered badly or the API call
        itself failed (timeout, throttling exhausted, etc.)."""
        try:
            resp = self.client.invoke(prompt=prompt, temperature=self.temperature,
                                       max_tokens=self.max_tokens)
            return {
                "text": resp.text,
                "model_id": getattr(resp, "model_id", self.model_id),
                "raw": resp.raw,
                "error": None,
            }
        except Exception as e:
            return {"text": "", "model_id": self.model_id, "raw": None, "error": str(e)}

    @staticmethod
    def _extract_json_text(raw_text: str) -> str:
        text = (raw_text or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        return text

    def _validate(self, raw_text: Optional[str], request_error: Optional[str],
                   fault_space: list[FaultCandidate], tried_ids: set):
        if request_error:
            return None, None, f"request_error: {request_error}"

        text = self._extract_json_text(raw_text)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            return None, None, f"malformed_json: {e}"
        if not isinstance(parsed, dict):
            return None, None, "response is not a JSON object"

        candidate_id = parsed.get("candidate_id")
        rationale = parsed.get("rationale")
        if not isinstance(candidate_id, str) or not candidate_id:
            return None, None, "missing or non-string 'candidate_id'"

        valid_ids = {c.id for c in fault_space}
        if candidate_id not in valid_ids:
            return None, None, f"candidate_id '{candidate_id}' is not in the fault space"
        if candidate_id in tried_ids:
            return None, None, (f"candidate_id '{candidate_id}' was already tried "
                                 f"earlier in this campaign")
        return candidate_id, rationale, None

    def _build_prompt(self, history: list[RunResult], fault_space: list[FaultCandidate],
                       not_tried: list[FaultCandidate], injection_number: int,
                       k_remaining: int, previous_error: Optional[str]) -> str:
        fault_space_json = json.dumps([
            {"id": c.id, "category": c.category, "target_service": c.target_service,
             "description": describe_candidate(c)}
            for c in fault_space
        ], indent=2)
        history_json = json.dumps([
            {
                "injection": idx + 1,
                "candidate_id": r.candidate.id,
                "category": r.candidate.category,
                "target_service": r.candidate.target_service,
                "weakness_signals": r.weakness_signals or None,
                "error": r.error,
            }
            for idx, r in enumerate(history)
        ], indent=2)
        not_tried_json = json.dumps([c.id for c in not_tried])
        error_block = ""
        if previous_error:
            error_block = (
                f"\nYour previous response was rejected: {previous_error}\n"
                f"Return ONLY the strict JSON object described below, choosing a "
                f"candidate_id from the not-yet-tried list above.\n"
            )

        prompt = self.prompt_template
        for token, value in (
            ("<<<CAMPAIGN_GOAL>>>", CAMPAIGN_GOAL),
            ("<<<INJECTION_NUMBER>>>", str(injection_number)),
            ("<<<K_TOTAL>>>", str(CAMPAIGN_K)),
            ("<<<K_REMAINING>>>", str(k_remaining)),
            ("<<<TOPOLOGY_SUMMARY>>>", self.topology_summary),
            ("<<<FAULT_SPACE_JSON>>>", fault_space_json),
            ("<<<HISTORY_JSON>>>", history_json if history else "[]"),
            ("<<<NOT_TRIED_IDS_JSON>>>", not_tried_json),
            ("<<<PREVIOUS_ERROR_BLOCK>>>", error_block),
        ):
            prompt = prompt.replace(token, value)
        return prompt

    def _log_transcript(self, **fields):
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), "arm": self.arm, **fields}
        self.campaign_dir.mkdir(parents=True, exist_ok=True)
        with open(self.transcript_path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")


STRATEGIES = {
    "random": RandomStrategy,
    "coverage": CoverageStrategy,
}


def strategy_choices() -> list[str]:
    """--strategy's argparse choices: the two non-LLM strategies plus one
    choice per frozen arm in experiments/llm-config.yaml (llm-claude,
    llm-llama, llm-mistral). Falls back to just the non-LLM choices if the
    config can't be read (keeps --help usable even with a broken/missing
    llm-config.yaml)."""
    choices = list(STRATEGIES.keys())
    try:
        choices += llm_arm_names()
    except Exception:
        pass
    return choices

################################################################################
# Manifest rendering
################################################################################

def _cm_pod_action(name: str, svc: str, action: str, extra: str = "", grace_period: bool = True) -> str:
    grace = "  gracePeriod: 0\n" if grace_period else ""
    return f"""apiVersion: chaos-mesh.org/v1alpha1
kind: PodChaos
metadata:
  name: {name}
  namespace: social-network
spec:
  action: {action}
  mode: one
{extra}  selector:
    namespaces:
      - social-network
    labelSelectors:
      app: {svc}
{grace}"""


def render_chaos_mesh_manifest(candidate: FaultCandidate, campaign: int, run_number: int) -> str:
    name = f"campaign-{candidate.id.lower()}-c{campaign}-r{run_number}"
    svc = candidate.target_service
    p = candidate.param
    t = candidate.scenario_template
    dur = chaoslib.CAMPAIGN_FAULT_DURATION

    if t == "pod-kill":
        return _cm_pod_action(name, svc, "pod-kill")
    if t == "container-kill":
        container = p.get("container", svc)
        return _cm_pod_action(name, svc, "container-kill", extra=f"  containerNames:\n    - {container}\n")
    if t == "pod-failure":
        return f"""apiVersion: chaos-mesh.org/v1alpha1
kind: PodChaos
metadata:
  name: {name}
  namespace: social-network
spec:
  action: pod-failure
  mode: one
  duration: "{dur}s"
  selector:
    namespaces:
      - social-network
    labelSelectors:
      app: {svc}
"""
    if t in ("latency-50ms", "latency-100ms", "latency-300ms"):
        latency = p["latency_ms"]
        jitter = p["jitter_ms"]
        corr = p.get("correlation", 50)
        return f"""apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: {name}
  namespace: social-network
spec:
  action: delay
  mode: all
  selector:
    namespaces:
      - social-network
    labelSelectors:
      app: {svc}
  delay:
    latency: "{latency}ms"
    jitter: "{jitter}ms"
    correlation: "{corr}"
  duration: "{dur}s"
"""
    if t == "packet-loss-5pct":
        loss = p.get("loss_pct", 5)
        corr = p.get("correlation", 25)
        return f"""apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: {name}
  namespace: social-network
spec:
  action: loss
  mode: all
  selector:
    namespaces:
      - social-network
    labelSelectors:
      app: {svc}
  loss:
    loss: "{loss}"
    correlation: "{corr}"
  duration: "{dur}s"
"""
    if t == "network-partition":
        direction = p.get("direction", "both")
        return f"""apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: {name}
  namespace: social-network
spec:
  action: partition
  mode: all
  selector:
    namespaces:
      - social-network
    labelSelectors:
      app: {svc}
  direction: {direction}
  duration: "{dur}s"
"""
    if t == "cpu-stress-80pct":
        workers = p.get("workers", 2)
        load = p.get("load_pct", 80)
        return f"""apiVersion: chaos-mesh.org/v1alpha1
kind: StressChaos
metadata:
  name: {name}
  namespace: social-network
spec:
  mode: all
  selector:
    namespaces:
      - social-network
    labelSelectors:
      app: {svc}
  stressors:
    cpu:
      workers: {workers}
      load: {load}
  duration: "{dur}s"
"""
    if t == "memory-pressure-80pct":
        workers = p.get("workers", 1)
        size = p.get("size_mb", 256)
        return f"""apiVersion: chaos-mesh.org/v1alpha1
kind: StressChaos
metadata:
  name: {name}
  namespace: social-network
spec:
  mode: all
  selector:
    namespaces:
      - social-network
    labelSelectors:
      app: {svc}
  stressors:
    memory:
      workers: {workers}
      size: "{size}MB"
  duration: "{dur}s"
"""
    if t in ("http-abort-503", "grpc-unavailable"):
        port = p.get("port", 8080 if t == "http-abort-503" else 9090)
        return f"""apiVersion: chaos-mesh.org/v1alpha1
kind: HTTPChaos
metadata:
  name: {name}
  namespace: social-network
spec:
  mode: all
  selector:
    namespaces:
      - social-network
    labelSelectors:
      app: {svc}
  target: Response
  abort: true
  port: {port}
  duration: "{dur}s"
"""
    raise ValueError(f"Unknown scenario_template '{t}' for chaos-mesh")


def _litmus_engine(name: str, svc: str, experiment_name: str, env: dict) -> str:
    env_lines = "\n".join(f'            - name: {k}\n              value: "{v}"' for k, v in env.items())
    return f"""apiVersion: litmuschaos.io/v1alpha1
kind: ChaosEngine
metadata:
  name: {name}
  namespace: social-network
spec:
  engineState: active
  appinfo:
    appns: social-network
    applabel: app={svc}
    appkind: deployment
  chaosServiceAccount: litmus-admin
  experiments:
    - name: {experiment_name}
      spec:
        components:
          env:
{env_lines}
"""


def litmus_fault_params(candidate: FaultCandidate) -> tuple[str, dict]:
    """Map a fault-space candidate to (litmus experiment/fault name, env dict).

    Single source of truth for the scenario_template -> real Litmus fault
    mapping, shared by the local manifest path (render_litmus_manifest,
    used by random/coverage strategies and chaos-mesh-style local execution)
    and the ChaosCenter API path (litmus_chaoscenter_client, used by the
    llm strategy's litmus arm -- see scripts/litmus_chaoscenter_client.py).
    Keeping one mapping means a scenario's parameters can never drift
    between the two execution paths.
    """
    svc = candidate.target_service
    p = candidate.param
    t = candidate.scenario_template
    dur = str(chaoslib.CAMPAIGN_FAULT_DURATION)

    if t in ("pod-kill", "pod-failure"):
        return "pod-delete", {
            "TOTAL_CHAOS_DURATION": dur, "CHAOS_INTERVAL": "10",
            "FORCE": "true", "PODS_AFFECTED_PERC": "100",
        }
    if t == "container-kill":
        container = p.get("container", svc)
        return "container-kill", {
            "TARGET_CONTAINER": container, "TOTAL_CHAOS_DURATION": dur,
            "CHAOS_INTERVAL": "10", "SOCKET_PATH": "/run/containerd/containerd.sock",
            "CONTAINER_RUNTIME": "containerd",
        }
    if t in ("latency-50ms", "latency-100ms", "latency-300ms"):
        return "pod-network-latency", {
            "TOTAL_CHAOS_DURATION": dur, "NETWORK_LATENCY": str(p["latency_ms"]),
            "JITTER": str(p["jitter_ms"]), "CONTAINER_RUNTIME": "containerd",
            "SOCKET_PATH": "/run/containerd/containerd.sock",
        }
    if t == "packet-loss-5pct":
        return "pod-network-loss", {
            "TOTAL_CHAOS_DURATION": dur,
            "NETWORK_PACKET_LOSS_PERCENTAGE": str(p.get("loss_pct", 5)),
            "CONTAINER_RUNTIME": "containerd", "SOCKET_PATH": "/run/containerd/containerd.sock",
        }
    if t == "network-partition":
        return "pod-network-partition", {
            "TOTAL_CHAOS_DURATION": dur, "CONTAINER_RUNTIME": "containerd",
            "SOCKET_PATH": "/run/containerd/containerd.sock",
        }
    if t == "cpu-stress-80pct":
        return "pod-cpu-hog-exec", {
            "TOTAL_CHAOS_DURATION": dur, "CPU_CORES": str(p.get("workers", 2)),
            "CPU_LOAD": str(p.get("load_pct", 80)), "PODS_AFFECTED_PERC": "100",
        }
    if t == "memory-pressure-80pct":
        return "pod-memory-hog-exec", {
            "TOTAL_CHAOS_DURATION": dur, "MEMORY_CONSUMPTION": str(p.get("size_mb", 256)),
            "PODS_AFFECTED_PERC": "100",
        }
    if t == "http-abort-503":
        return "pod-http-status-code", {
            "TOTAL_CHAOS_DURATION": dur, "STATUS_CODE": str(p.get("status_code", 503)),
            "MODIFY_RESPONSE_BODY": "true", "RESPONSE_BODY": "service unavailable",
            "TARGET_SERVICE_PORT": str(p.get("port", 8080)), "TOXICS_PERIOD": dur,
            "CONTAINER_RUNTIME": "containerd", "SOCKET_PATH": "/run/containerd/containerd.sock",
        }
    if t == "grpc-unavailable":
        return "pod-http-status-code", {
            "TOTAL_CHAOS_DURATION": dur, "STATUS_CODE": "503",
            "TARGET_SERVICE_PORT": str(p.get("port", 9090)),
            "CONTAINER_RUNTIME": "containerd", "SOCKET_PATH": "/run/containerd/containerd.sock",
        }
    raise ValueError(f"Unknown scenario_template '{t}' for litmus")


def render_litmus_manifest(candidate: FaultCandidate, campaign: int, run_number: int) -> str:
    name = f"campaign-{candidate.id.lower()}-c{campaign}-r{run_number}"
    experiment_name, env = litmus_fault_params(candidate)
    return _litmus_engine(name, candidate.target_service, experiment_name, env)


def render_manifest(tool: str, candidate: FaultCandidate, campaign: int, run_number: int) -> str:
    if tool == "chaos-mesh":
        return render_chaos_mesh_manifest(candidate, campaign, run_number)
    if tool == "litmus":
        return render_litmus_manifest(candidate, campaign, run_number)
    raise ValueError(f"Unknown tool '{tool}'")

################################################################################
# Weakness signals
################################################################################

def _avg_metric(metrics_data: dict, key: str) -> float:
    total, count = 0.0, 0
    for series in metrics_data.get(key, []):
        for _, val in series.get("values", []):
            total += float(val)
            count += 1
    return total / count if count > 0 else 0.0


def _last_metric(metrics_data: dict, key: str) -> float:
    last = 0.0
    for series in metrics_data.get(key, []):
        values = series.get("values", [])
        if values:
            last = max(last, float(values[-1][1]))
    return last


def compute_weakness_signals(protocol_results: dict, running_p99_median: Optional[float]) -> dict:
    """SLO-violation flags consumed by discovery metrics later. Two of the
    four (error_rate, pod_restarts) come straight from data run-experiment.py
    already collects. The other two are approximations forced by the
    protocol's single wrk2 job spanning baseline+fault+recovery (there is no
    separate per-phase client-side latency measurement, matching the
    limitation already present in --mode benchmark):

      - p99_over_3x_baseline: compares this injection's aggregate wrk2 p99
        against the RUNNING MEDIAN of prior injections' aggregate p99 in the
        same campaign, not a true within-injection baseline-phase-only
        figure. None (not a violation) for the first injection, since there
        is no running median yet.
      - recovery_over_60s: approximated as "container CPU at the end of the
        recovery phase is still >20% above the baseline-phase average",
        i.e. the system had not visibly settled by the time the fixed 60s
        recovery window ended -- not a measured wall-clock TTR.
    """
    wrk2 = protocol_results.get("wrk2", {})
    derived = protocol_results.get("derived", {})
    errors = wrk2.get("errors", {})
    requests_total = wrk2.get("requests_total", 0) or 0
    error_count = sum(errors.get(k, 0) for k in ("connect", "read", "write", "timeout", "http_non2xx3xx"))
    error_rate = (error_count / requests_total) if requests_total else 0.0

    phases = protocol_results.get("phases", {})
    baseline_metrics = phases.get("baseline", {}).get("infra_metrics", {})
    recovery_metrics = phases.get("recovery", {}).get("infra_metrics", {})

    baseline_cpu = _avg_metric(baseline_metrics, "cpu_usage")
    recovery_cpu_end = _last_metric(recovery_metrics, "cpu_usage")
    recovery_over_60s = bool(baseline_cpu > 0 and recovery_cpu_end > baseline_cpu * 1.2)

    p99_ms = wrk2.get("latency_ms", {}).get("p99", 0.0)
    p99_over_3x_baseline = None
    if running_p99_median is not None and running_p99_median > 0:
        p99_over_3x_baseline = p99_ms > 3 * running_p99_median

    pod_restarts = derived.get("pod_restarts_during_fault", 0)

    signals = {
        "error_rate": round(error_rate, 4),
        "error_rate_violation": error_rate > 0.05,
        "p99_ms": p99_ms,
        "running_p99_median_ms": running_p99_median,
        "p99_over_3x_baseline": p99_over_3x_baseline,
        "recovery_over_60s": recovery_over_60s,
        "pod_restarts": pod_restarts,
        "pod_restarts_violation": pod_restarts > 0,
    }
    signals["any_violation"] = bool(
        signals["error_rate_violation"]
        or signals["p99_over_3x_baseline"]
        or signals["recovery_over_60s"]
        or signals["pod_restarts_violation"]
    )
    return signals

################################################################################
# Campaign orchestration
################################################################################

def run_campaign(tool: str, strategy_name: str, campaign_number: int, seed: Optional[int],
                  dry_run: bool = False, llm_client=None):
    fault_space = load_fault_space()

    campaign_dir = chaoslib.DATA_DIR / "campaigns" / strategy_name / f"campaign-{campaign_number}"
    campaign_dir.mkdir(parents=True, exist_ok=True)

    if strategy_name == "random":
        if seed is None:
            print("ERROR: --strategy random requires --seed", file=sys.stderr)
            sys.exit(1)
        strategy = RandomStrategy(seed)
    elif strategy_name in STRATEGIES:
        strategy = STRATEGIES[strategy_name]()
    else:
        # Not "random", not "coverage" -> must be one of the frozen LLM arms
        # (llm-claude / llm-llama / llm-mistral); argparse's --strategy
        # choices already reject anything else.
        llm_config = load_llm_config()
        model_id = llm_model_id(strategy_name, llm_config)
        selection_cfg = llm_config.get("component3_selection", {})
        client = llm_client or llmclient.BedrockClient(
            model_id=model_id, region=llm_config.get("region", llmclient.DEFAULT_REGION))
        strategy = LLMStrategy(
            arm=strategy_name, client=client, campaign_dir=campaign_dir, model_id=model_id,
            temperature=selection_cfg.get("temperature", 0.2),
            max_tokens=selection_cfg.get("max_tokens", 1024),
            # Fallback RNG seed: --seed if given, else the campaign number,
            # so a resumed campaign's fallback picks (if any) reproduce
            # rather than depending on the number of prior calls.
            fallback_seed=seed if seed is not None else campaign_number,
        )

    # Reconstruct history from any injections already run (resume support).
    history: list[RunResult] = []
    for i in range(1, CAMPAIGN_K + 1):
        injection_file = campaign_dir / f"injection-{i}.json"
        if not injection_file.exists():
            break
        with open(injection_file) as f:
            saved = json.load(f)
        candidate = candidate_by_id(fault_space, saved["metadata"]["candidate_id"])
        history.append(RunResult(
            candidate=candidate,
            weakness_signals=saved.get("weakness_signals"),
            error=saved.get("error"),
        ))

    if len(history) >= CAMPAIGN_K:
        print(f"  Campaign {strategy_name}/campaign-{campaign_number} already has all "
              f"{CAMPAIGN_K} injections. Nothing to do.")
        write_campaign_summary(campaign_dir, tool, strategy_name, campaign_number, seed, fault_space,
                                llm_fallback_count=getattr(strategy, "fallback_count", None))
        return

    if dry_run:
        # Show what the remaining selections would be without touching the
        # cluster or advancing any strategy state definitively.
        preview_history = list(history)
        for i in range(len(history) + 1, CAMPAIGN_K + 1):
            candidate = strategy.select(preview_history, fault_space, CAMPAIGN_K - i + 1)
            print(f"  [DRY RUN] injection {i}: would select {candidate.id} "
                  f"({candidate.scenario_template} -> {candidate.target_service})")
            preview_history.append(RunResult(candidate=candidate, weakness_signals={}))
        return

    prom_available = False
    try:
        chaoslib.start_port_forward("monitoring", "prometheus-kube-prometheus-prometheus",
                                     chaoslib.PROMETHEUS_PORT, 9090)
        prom_available = True
    except RuntimeError as e:
        print(f"  WARNING: {e}. Prometheus metrics will be unavailable for this campaign.", file=sys.stderr)

    try:
        p99_values = [h.weakness_signals["p99_ms"] for h in history
                      if h.weakness_signals and h.weakness_signals.get("p99_ms")]

        for i in range(len(history) + 1, CAMPAIGN_K + 1):
            k_remaining = CAMPAIGN_K - i + 1
            candidate = strategy.select(history, fault_space, k_remaining)

            print(f"\n{'='*80}")
            print(f"  Campaign {strategy_name}/campaign-{campaign_number} - injection {i}/{CAMPAIGN_K}")
            print(f"  Candidate: {candidate.id} ({candidate.scenario_template} -> {candidate.target_service})")
            print(f"{'='*80}")

            manifest_yaml = render_manifest(tool, candidate, campaign_number, i)
            manifest_path = campaign_dir / f"_manifest-injection-{i}.yaml"
            manifest_path.write_text(manifest_yaml)

            running_p99_median = None
            if p99_values:
                sorted_p99 = sorted(p99_values)
                mid = len(sorted_p99) // 2
                running_p99_median = (sorted_p99[mid] if len(sorted_p99) % 2
                                       else (sorted_p99[mid - 1] + sorted_p99[mid]) / 2)

            # Per-injection state reset, same rationale as the batch runners:
            # weakness signals are only comparable across injections (and
            # campaigns) from identical app state. CHAOS_RESET_STATE=0 disables.
            if os.environ.get("CHAOS_RESET_STATE", "1") == "1" and not dry_run:
                reset_script = Path(__file__).resolve().parent / "reset-app-state.sh"
                reset_rc = subprocess.run(
                    ["bash", str(reset_script)],
                    capture_output=True, text=True,
                ).returncode
                if reset_rc != 0:
                    print(f"  WARNING: state reset failed (rc={reset_rc}); "
                          f"recording and continuing", file=sys.stderr)

            injection_record = {
                "metadata": {
                    "tool": tool,
                    "strategy": strategy_name,
                    "campaign": campaign_number,
                    "injection": i,
                    "candidate_id": candidate.id,
                    "scenario_template": candidate.scenario_template,
                    "category": candidate.category,
                    "target_service": candidate.target_service,
                    "param": candidate.param,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "protocol": {
                        "baseline_s": chaoslib.CAMPAIGN_BASELINE_DURATION,
                        "fault_s": chaoslib.CAMPAIGN_FAULT_DURATION,
                        "recovery_s": chaoslib.CAMPAIGN_RECOVERY_DURATION,
                        "cooldown_s": 0,
                    },
                },
                "wrk2": {},
                "phases": {},
                "derived": {},
                "weakness_signals": {},
            }
            window = None
            weakness_signals = {}

            try:
                protocol_results = chaoslib.run_fault_protocol(
                    experiment_path=manifest_path,
                    label=f"campaign-{candidate.id.lower()}",
                    run_number=i,
                    baseline_s=chaoslib.CAMPAIGN_BASELINE_DURATION,
                    fault_s=chaoslib.CAMPAIGN_FAULT_DURATION,
                    recovery_s=chaoslib.CAMPAIGN_RECOVERY_DURATION,
                    cooldown_s=0,
                    prom_available=prom_available,
                    post_fault_restart=(candidate.target_service
                                         if candidate.scenario_template in ("http-abort-503", "grpc-unavailable")
                                         else None),
                )
                window = protocol_results.pop("_window", None)
                injection_record.update(protocol_results)
                weakness_signals = compute_weakness_signals(protocol_results, running_p99_median)
                injection_record["weakness_signals"] = weakness_signals
                if weakness_signals.get("p99_ms"):
                    p99_values.append(weakness_signals["p99_ms"])
            except KeyboardInterrupt:
                print("\n  Interrupted by user. Cleaning up...")
                chaoslib.kubectl_delete_file(str(manifest_path))
                injection_record["error"] = "interrupted"
            except Exception as e:
                print(f"\n  ERROR: {e}", file=sys.stderr)
                injection_record["error"] = str(e)
            finally:
                try:
                    manifest_path.unlink(missing_ok=True)
                except Exception:
                    pass

            injection_file = campaign_dir / f"injection-{i}.json"
            with open(injection_file, "w") as f:
                json.dump(injection_record, f, indent=2, default=str)
            print(f"\n  Injection {i} results saved to: {injection_file}")

            if window is not None:
                chaoslib.write_timeseries_sidecar(
                    injection_file, window["start"], window["end"],
                    window["fault_start"], window["fault_end"],
                    prom_available=prom_available,
                )

            history.append(RunResult(candidate=candidate, weakness_signals=weakness_signals,
                                      error=injection_record.get("error")))
    finally:
        chaoslib.cleanup_port_forwards()

    write_campaign_summary(campaign_dir, tool, strategy_name, campaign_number, seed, fault_space,
                            llm_fallback_count=getattr(strategy, "fallback_count", None))


def write_campaign_summary(campaign_dir: Path, tool: str, strategy_name: str, campaign_number: int,
                            seed: Optional[int], fault_space: list[FaultCandidate],
                            llm_fallback_count: Optional[int] = None):
    injections = []
    for i in range(1, CAMPAIGN_K + 1):
        injection_file = campaign_dir / f"injection-{i}.json"
        if not injection_file.exists():
            continue
        with open(injection_file) as f:
            saved = json.load(f)
        injections.append({
            "injection": i,
            "candidate_id": saved["metadata"]["candidate_id"],
            "scenario_template": saved["metadata"]["scenario_template"],
            "category": saved["metadata"]["category"],
            "target_service": saved["metadata"]["target_service"],
            "error": saved.get("error"),
            "weakness_signals": saved.get("weakness_signals", {}),
        })

    violation_counts = Counter()
    for rec in injections:
        ws = rec["weakness_signals"] or {}
        for flag in ("error_rate_violation", "p99_over_3x_baseline", "recovery_over_60s", "pod_restarts_violation"):
            if ws.get(flag):
                violation_counts[flag] += 1
        if ws.get("any_violation"):
            violation_counts["any_violation"] += 1

    summary = {
        "metadata": {
            "tool": tool,
            "strategy": strategy_name,
            "campaign": campaign_number,
            "seed": seed,
            "k": CAMPAIGN_K,
            "fault_space_size": len(fault_space),
            "generated": datetime.now(timezone.utc).isoformat(),
        },
        "injections_completed": len(injections),
        "violation_counts": dict(violation_counts),
        "injections": injections,
    }
    # Only present for LLM-arm campaigns: how many of the CAMPAIGN_K
    # selections fell back to a seeded-random pick after MAX_REPROMPTS
    # invalid model responses (see LLMStrategy). Reported as a metric.
    if llm_fallback_count is not None:
        summary["metadata"]["llm_fallback_count"] = llm_fallback_count
    summary_file = campaign_dir / "campaign-summary.json"
    with open(summary_file, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\n  Campaign summary saved to: {summary_file}")

################################################################################
# Entry point
################################################################################

def parse_args():
    parser = argparse.ArgumentParser(
        description="Chaos Benchmark Campaign Runner (ML fault-selection study scaffold)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --tool chaos-mesh --strategy random --seed 42 --campaign 1
  %(prog)s --tool litmus --strategy coverage --campaign 1
  %(prog)s --tool chaos-mesh --strategy llm-claude --campaign 1
  %(prog)s --tool chaos-mesh --strategy random --seed 42 --campaign 1 --dry-run
        """,
    )
    parser.add_argument("--tool", required=True, choices=["chaos-mesh", "litmus"],
                        help="Chaos engineering tool")
    parser.add_argument("--strategy", required=True, choices=strategy_choices(),
                        help="Fault-selection strategy: random, coverage, or one of the "
                             "frozen LLM arms in experiments/llm-config.yaml "
                             "(llm-claude / llm-llama / llm-mistral)")
    parser.add_argument("--campaign", required=True, type=int,
                        help="Campaign number (distinguishes repeated campaign runs of the same strategy)")
    parser.add_argument("--seed", type=int, default=None,
                        help="RNG seed (required for --strategy random)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the selections that would be made without touching the cluster")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.campaign < 1:
        print(f"ERROR: --campaign must be >= 1, got {args.campaign}", file=sys.stderr)
        sys.exit(1)
    run_campaign(args.tool, args.strategy, args.campaign, args.seed, args.dry_run)


if __name__ == "__main__":
    main()
