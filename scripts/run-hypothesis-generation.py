#!/usr/bin/env python3
"""
Component 4: LLM hypothesis-generation study (analysis/PREREGISTRATION.md).

For each of the 36 fault-space candidates x each of the 3 frozen Bedrock
models (experiments/llm-config.yaml) x 5 samples, asks the model to predict
-- BEFORE the fault is run -- (i) throughput direction, (ii) the set of
services whose p99 will exceed 3x baseline, (iii) whether any
resilience-weakness signal will fire. Output is scored later, mechanically,
against Component 1 ground truth once it exists; this script only generates
and validates the raw predictions (540 generations at full scale: 3 x 36 x 5).

STATUS: generation-only, NOT yet runnable for real. Steady-state baseline
telemetry (medians per service from Component 1's baseline windows) does not
exist until Component 1 data collection finishes -- see
analysis/PREREGISTRATION.md ("this component runs after Component 1 data
exists"). `load_baseline_summary()` below is the integration point: wire it
up to read real per-service baseline medians once Component 1 data lands,
and this script is ready to run against Bedrock. Until then it raises
NotImplementedError outside of --dry-run, which is the intended guard: this
study's frozen sample budget (component4_hypothesis's temperature is 0.7,
not 0, so repeat runs are not free reproductions of the same result) must
not be spent before there is ground truth to score it against.

--dry-run runs a 2-candidate x 3-arm x 1-sample smoke test end to end
against a zero-network mock client with a clearly-labeled SYNTHETIC baseline,
and writes to a temp directory -- never data-v2/ -- so it's safe to run
anytime without touching the live study's output tree.

Usage:
  python3 run-hypothesis-generation.py --dry-run
  python3 run-hypothesis-generation.py --resume   (real mode; currently
                                                     raises NotImplementedError,
                                                     see STATUS above)

Output (real mode, per experiments/llm-config.yaml's component4_hypothesis):
  data-v2/ml/hypotheses/{arm}/candidate-{id}/sample-{n}.json
  (override root via CHAOS_HYPOTHESIS_DIR; each file embeds its own full
  request/response transcript -- prompt, every retry attempt's raw
  completion and raw API response, validation outcome)
Output (--dry-run):
  a temp directory, printed at the end of the run.
"""

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

import llmclient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXPERIMENTS_DIR = PROJECT_ROOT / "experiments"
PROMPTS_DIR = PROJECT_ROOT / "scripts" / "prompts"

FAULT_SPACE_FILE = EXPERIMENTS_DIR / "fault-space.yaml"
LLM_CONFIG_FILE = EXPERIMENTS_DIR / "llm-config.yaml"
HYPOTHESIS_PROMPT_FILE = PROMPTS_DIR / "hypothesis.txt"
HYPOTHESIS_SCHEMA_FILE = PROMPTS_DIR / "hypothesis-schema.json"
TOPOLOGY_FILE = PROMPTS_DIR / "topology-social-network.md"

DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "data-v2" / "ml" / "hypotheses"
MAX_REPROMPTS = 3

################################################################################
# Fault space (standalone loader -- run-campaign.py's filename has a hyphen
# too, so it can't be `import`-ed either; duplicating this small loader
# keeps this script self-contained rather than reaching into another
# hyphenated script via importlib tricks for one function).
################################################################################

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


def describe_candidate(candidate: dict) -> str:
    template = _DESCRIPTION_TEMPLATES.get(candidate["scenario_template"])
    svc = candidate["target_service"]
    if template is None:
        return f"{candidate['scenario_template']} fault against {svc}"
    p = candidate.get("param") or {}
    return template.format(
        svc=svc,
        container=p.get("container", svc),
        direction=p.get("direction", "both"),
        workers=p.get("workers", "?"),
        size=p.get("size_mb", "?"),
    )


def load_fault_space(path: Path = FAULT_SPACE_FILE) -> tuple[list[dict], dict]:
    with open(path) as f:
        doc = yaml.safe_load(f)
    return doc["candidates"], doc["metadata"]


def load_llm_config(path: Path = LLM_CONFIG_FILE) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_hypothesis_schema(path: Path = HYPOTHESIS_SCHEMA_FILE) -> dict:
    with open(path) as f:
        return json.load(f)

################################################################################
# Steady-state baseline input (Component 1 integration point, see module
# docstring's STATUS section)
################################################################################

SYNTHETIC_DRY_RUN_NOTE = ("SYNTHETIC placeholder baseline for --dry-run only. "
                           "Not derived from any real run; Component 1 has not "
                           "produced ground truth yet.")


def _synthetic_baseline(services: list[str]) -> dict:
    return {
        "note": SYNTHETIC_DRY_RUN_NOTE,
        "aggregate_throughput_rps": 200.0,
        "per_service_p50_latency_ms": {svc: 12.0 for svc in services},
        "per_service_p99_latency_ms": {svc: 45.0 for svc in services},
    }


def load_baseline_summary(candidate: dict, services: list[str], dry_run: bool) -> dict:
    """Steady-state baseline telemetry input for Component 4's prompt:
    per-service median/p99 latency and aggregate throughput with no fault
    active. Component 1 (analysis/PREREGISTRATION.md) is the only
    legitimate source for this and has not produced results yet.
    --dry-run uses a clearly-labeled synthetic placeholder so the
    prompt-building and validation path is fully testable before Component 1
    exists; real mode raises until this function is wired up to Component 1
    data (do not remove that guard, see the module docstring)."""
    if dry_run:
        return _synthetic_baseline(services)
    raise NotImplementedError(
        "load_baseline_summary(): Component 1 ground truth does not exist yet "
        "(analysis/PREREGISTRATION.md, 'this component runs after Component 1 "
        "data exists'). Wire this up to read real per-service baseline-phase "
        "medians once Component 1 data lands, then this script is ready to "
        "run against Bedrock for real."
    )

################################################################################
# Validation (hand-rolled against hypothesis-schema.json; no external
# jsonschema dependency, matching this repo's stdlib-first convention)
################################################################################

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


def validate_hypothesis_response(raw_text: str, schema: dict) -> tuple[Optional[dict], Optional[str]]:
    text = _extract_json_text(raw_text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"malformed_json: {e}"
    if not isinstance(parsed, dict):
        return None, "response is not a JSON object"

    required = schema.get("required", [])
    missing = [k for k in required if k not in parsed]
    if missing:
        return None, f"missing required field(s): {missing}"

    props = schema.get("properties", {})

    td_enum = props.get("throughput_direction", {}).get("enum", [])
    if parsed.get("throughput_direction") not in td_enum:
        return None, f"throughput_direction must be one of {td_enum}"

    svc_enum = props.get("degraded_services", {}).get("items", {}).get("enum", [])
    degraded = parsed.get("degraded_services")
    if not isinstance(degraded, list) or any(not isinstance(s, str) or s not in svc_enum for s in degraded):
        return None, f"degraded_services must be a list drawn from {svc_enum}"

    if not isinstance(parsed.get("weakness_signal_present"), bool):
        return None, "weakness_signal_present must be a boolean"

    rationale = parsed.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        return None, "rationale must be a non-empty string"

    if schema.get("additionalProperties") is False:
        extra = sorted(set(parsed) - set(props))
        if extra:
            return None, f"unexpected field(s): {extra}"

    return parsed, None

################################################################################
# Prompt building
################################################################################

def build_prompt(template: str, topology: str, baseline: dict, candidate: dict,
                  previous_error: Optional[str]) -> str:
    candidate_json = json.dumps({
        "id": candidate["id"],
        "category": candidate["category"],
        "target_service": candidate["target_service"],
        "description": describe_candidate(candidate),
    }, indent=2)
    baseline_json = json.dumps(baseline, indent=2)
    error_block = ""
    if previous_error:
        error_block = (
            f"\nYour previous response was rejected: {previous_error}\n"
            f"Return ONLY the strict JSON object described below.\n"
        )
    prompt = template
    for token, value in (
        ("<<<TOPOLOGY_SUMMARY>>>", topology),
        ("<<<BASELINE_SUMMARY_JSON>>>", baseline_json),
        ("<<<CANDIDATE_JSON>>>", candidate_json),
        ("<<<PREVIOUS_ERROR_BLOCK>>>", error_block),
    ):
        prompt = prompt.replace(token, value)
    return prompt

################################################################################
# Mock client (--dry-run only, zero network)
################################################################################

@dataclass
class _MockResponse:
    text: str
    model_id: str
    raw: dict


class DryRunMockClient:
    """Deterministic, zero-network stand-in for llmclient.BedrockClient used
    only by --dry-run. Always returns a schema-valid prediction so the
    smoke test exercises the full write path without needing to also
    simulate retries (LLMStrategy's test suite,
    scripts/tests/test_llm_strategy.py, covers the retry/validation logic
    in depth already)."""

    def __init__(self, model_id: str):
        self.model_id = model_id

    def invoke(self, prompt: str, temperature: float, max_tokens: float) -> _MockResponse:
        prediction = {
            "throughput_direction": "degrade",
            "degraded_services": [],
            "weakness_signal_present": False,
            "rationale": "[DRY RUN mock response, not a real model prediction]",
        }
        return _MockResponse(text=json.dumps(prediction), model_id=self.model_id,
                              raw={"mock": True, "dry_run": True})

################################################################################
# Generation
################################################################################

def generate_one(client, template: str, topology: str, baseline: dict, candidate: dict,
                  schema: dict, temperature: float, max_tokens: int) -> dict:
    attempts = []
    prediction = None
    previous_error = None
    for attempt in range(1, MAX_REPROMPTS + 1):
        prompt = build_prompt(template, topology, baseline, candidate, previous_error)
        try:
            resp = client.invoke(prompt=prompt, temperature=temperature, max_tokens=max_tokens)
            raw_completion, raw_response, model_id, request_error = resp.text, resp.raw, resp.model_id, None
        except Exception as e:
            raw_completion, raw_response, model_id, request_error = "", None, getattr(client, "model_id", None), str(e)

        if request_error:
            validation_outcome = f"invalid: request_error: {request_error}"
            previous_error = f"request_error: {request_error}"
        else:
            parsed, validation_error = validate_hypothesis_response(raw_completion, schema)
            if validation_error is None:
                prediction = parsed
                validation_outcome = "valid"
            else:
                validation_outcome = f"invalid: {validation_error}"
                previous_error = validation_error

        attempts.append({
            "attempt": attempt,
            "prompt": prompt,
            "raw_completion": raw_completion,
            "raw_response": raw_response,
            "model_id": model_id,
            "validation_outcome": validation_outcome,
        })
        if prediction is not None:
            break

    return {
        "attempts": attempts,
        "prediction": prediction,
        "valid": prediction is not None,
    }


def run_generation(dry_run: bool, resume: bool):
    fault_space, metadata = load_fault_space()
    llm_config = load_llm_config()
    arms = [m["arm"] for m in llm_config["models"]]
    hconfig = llm_config.get("component4_hypothesis", {})
    temperature = hconfig.get("temperature", 0.7)
    max_tokens = hconfig.get("max_tokens", 2048)
    samples_per_candidate = hconfig.get("samples_per_candidate", 5)
    schema = load_hypothesis_schema()
    template = HYPOTHESIS_PROMPT_FILE.read_text()
    topology = TOPOLOGY_FILE.read_text()

    if dry_run:
        candidates = fault_space[:2]
        samples_per_candidate = 1
        output_root = Path(tempfile.mkdtemp(prefix="chaos-hyp-dryrun-"))
        clients = {arm: DryRunMockClient(model_id=llm_model_id(arm, llm_config)) for arm in arms}
        print(f"[DRY RUN] {len(candidates)} candidates x {len(arms)} arms x "
              f"{samples_per_candidate} sample -- writing to temp dir "
              f"(never data-v2/): {output_root}")
    else:
        candidates = fault_space
        output_root = Path(os.environ.get("CHAOS_HYPOTHESIS_DIR", str(DEFAULT_OUTPUT_ROOT)))
        clients = {arm: llmclient.BedrockClient(model_id=llm_model_id(arm, llm_config),
                                                 region=llm_config.get("region", llmclient.DEFAULT_REGION))
                   for arm in arms}
        print(f"{len(candidates)} candidates x {len(arms)} arms x "
              f"{samples_per_candidate} samples -- writing to {output_root}")

    services = metadata["services"]
    total_generated, total_skipped = 0, 0

    for candidate in candidates:
        baseline = load_baseline_summary(candidate, services, dry_run=dry_run)
        for arm in arms:
            out_dir = output_root / arm / f"candidate-{candidate['id']}"
            for sample in range(1, samples_per_candidate + 1):
                out_file = out_dir / f"sample-{sample}.json"
                if resume and out_file.exists():
                    total_skipped += 1
                    continue

                result = generate_one(clients[arm], template, topology, baseline, candidate,
                                       schema, temperature, max_tokens)
                record = {
                    "metadata": {
                        "arm": arm,
                        "model_id": llm_model_id(arm, llm_config),
                        "candidate_id": candidate["id"],
                        "sample": sample,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        "dry_run": dry_run,
                        "generated": datetime.now(timezone.utc).isoformat(),
                    },
                    "baseline_summary": baseline,
                    "candidate": {
                        "id": candidate["id"], "category": candidate["category"],
                        "target_service": candidate["target_service"],
                        "description": describe_candidate(candidate),
                    },
                    **result,
                }
                out_dir.mkdir(parents=True, exist_ok=True)
                with open(out_file, "w") as f:
                    json.dump(record, f, indent=2, default=str)
                total_generated += 1

    print(f"\nGenerated {total_generated} hypothesis sample(s)"
          + (f", skipped {total_skipped} existing (--resume)" if resume else "")
          + f" under {output_root}")
    return output_root


def llm_model_id(arm: str, config: dict) -> str:
    for m in config["models"]:
        if m["arm"] == arm:
            return m["model_id"]
    raise KeyError(f"arm '{arm}' not found in {LLM_CONFIG_FILE}")

################################################################################
# Entry point
################################################################################

def parse_args():
    parser = argparse.ArgumentParser(
        description="Component 4: LLM hypothesis-generation study (generation only, see module docstring for STATUS)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --dry-run
  %(prog)s --dry-run --resume
        """,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="2 candidates x 3 arms x 1 sample against a mock client, "
                             "writes to a temp dir (never data-v2/)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip sample files that already exist")
    return parser.parse_args()


def main():
    args = parse_args()
    if not args.dry_run:
        print("ERROR: real (non-dry-run) generation is blocked until Component 1 "
              "ground truth exists -- see load_baseline_summary()'s NotImplementedError "
              "and the module docstring's STATUS section.", file=sys.stderr)
    try:
        run_generation(dry_run=args.dry_run, resume=args.resume)
    except NotImplementedError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
