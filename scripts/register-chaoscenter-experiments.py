#!/usr/bin/env -S python3 -u
"""Register the 36 fault-space candidates as ChaosCenter experiments.

The experiments form a catalog only. Campaign injections run from local
manifests (run-campaign.py, chaoslib.run_fault_protocol), and the registered
workflows call a `chaos-runner` command that the litmuschaos/go-runner image
does not contain (see litmus_chaoscenter_client.py).

Writes the {candidate_id: experimentID} mapping to
experiments/chaoscenter-experiment-ids.json, which run-campaign.py's optional
catalog check reads. Safe to re-run: a candidate is skipped when the
existing mapping has an id for it that ChaosCenter still lists.

Needs CHAOS_DATA_DIR set (it imports run-campaign.py, which imports chaoslib)
and the ChaosCenter ports forwarded to localhost.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
OUTPUT_PATH = REPO_ROOT / "experiments" / "chaoscenter-experiment-ids.json"


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    run_campaign = _load_module("run_campaign", "run-campaign.py")
    from litmus_chaoscenter_client import ChaosCenterClient

    fault_space = run_campaign.load_fault_space()
    litmus_candidates = [c for c in fault_space]  # every candidate has a LitmusChaos mapping (litmus_fault_params)

    client = ChaosCenterClient()
    existing = {e["experimentID"] for e in client.list_experiments()}
    print(f"ChaosCenter currently has {len(existing)} registered experiments")

    mapping: dict[str, str] = {}
    if OUTPUT_PATH.exists():
        mapping = json.loads(OUTPUT_PATH.read_text())
        print(f"Loaded {len(mapping)} previously-registered candidate IDs from {OUTPUT_PATH.name}")

    registered, skipped, failed = 0, 0, 0
    for candidate in litmus_candidates:
        if candidate.id in mapping and mapping[candidate.id] in existing:
            skipped += 1
            continue
        try:
            fault_name, env = run_campaign.litmus_fault_params(candidate)
        except ValueError as e:
            print(f"  SKIP {candidate.id}: {e}")
            failed += 1
            continue
        description = run_campaign.describe_candidate(candidate)
        try:
            experiment_id = client.create_chaos_experiment(
                name=f"cb-{candidate.id.lower()}",
                fault_name=fault_name,
                target_service=candidate.target_service,
                env=env,
                description=description,
            )
        except Exception as e:
            print(f"  FAIL {candidate.id}: {e}")
            failed += 1
            continue
        mapping[candidate.id] = experiment_id
        registered += 1
        print(f"  OK {candidate.id} -> {experiment_id} ({fault_name} on {candidate.target_service})")

    OUTPUT_PATH.write_text(json.dumps(mapping, indent=2, sort_keys=True) + "\n")

    live = client.list_experiments()
    print(f"\nRegistered this run: {registered}, skipped (already present): {skipped}, failed: {failed}")
    print(f"ChaosCenter now reports {len(live)} total experiments; mapping has {len(mapping)} entries")
    print(f"Mapping written to {OUTPUT_PATH}")

    if len(mapping) != len(litmus_candidates):
        print(f"WARNING: {len(litmus_candidates)} fault-space candidates but only "
              f"{len(mapping)} registered -- see failures above", file=sys.stderr)


if __name__ == "__main__":
    main()
