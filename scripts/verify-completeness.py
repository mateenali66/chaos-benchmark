#!/usr/bin/env -S python3 -u
"""Completeness verifier for the revision campaign.

Checks the ACTUAL result files on disk against the EXPECTED experiment matrix
so that a missing or corrupt run is caught at stage end, not at analysis time
(February-campaign lesson: silently missed tests surface months later).

Checks per expected run:
  1. result JSON exists, parses, and has the required top-level keys
     (metadata, wrk2, phases, derived)
  2. timeseries sidecar exists and gunzips to valid JSON (unless --no-sidecar)

Usage:
  verify-completeness.py --data-dir data-v2/bench-a --mode overhead --tool chaos-mesh --stages baseline
  verify-completeness.py --data-dir data-v2/bench-a --mode overhead --tool chaos-mesh --stages baseline,idle,fault
  verify-completeness.py --data-dir data-v2/bench-a --mode benchmark --tool chaos-mesh --reps 30
  verify-completeness.py --data-dir data-v2/ml --mode campaign --strategies random,coverage,llm-claude,llm-llama,llm-mistral

Exit 0 = complete and valid. Exit 1 = gaps/corruption (each listed on stdout).
"""

import argparse
import gzip
import json
import sys
from pathlib import Path

SCENARIOS = ["p1", "p2", "p3", "n1", "n2", "n3", "n4", "n5", "r1", "r2", "a1", "a2"]
REQUIRED_KEYS = {"metadata", "wrk2", "phases", "derived"}


def check_run(json_path: Path, problems: list, no_sidecar: bool) -> None:
    if not json_path.exists():
        problems.append(f"MISSING  {json_path}")
        return
    try:
        data = json.loads(json_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        problems.append(f"CORRUPT  {json_path}: {exc}")
        return
    missing = REQUIRED_KEYS - set(data)
    if missing:
        problems.append(f"BADKEYS  {json_path}: missing {sorted(missing)}")
    if no_sidecar:
        return
    sidecar = json_path.with_name(json_path.name.replace(".json", ".timeseries.json.gz"))
    if not sidecar.exists():
        problems.append(f"NOSIDE   {sidecar}")
        return
    try:
        with gzip.open(sidecar, "rt") as fh:
            json.load(fh)
    except Exception as exc:  # corrupt gzip or JSON both count
        problems.append(f"BADSIDE  {sidecar}: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--mode", required=True, choices=["benchmark", "overhead", "campaign"])
    ap.add_argument("--tool", choices=["chaos-mesh", "litmus"])
    ap.add_argument("--start-rep", type=int, default=1,
                    help="benchmark mode: first rep to expect (matches "
                         "run-all-experiments.sh's --start-rep). Needed "
                         "because the crossover design means a cluster's "
                         "two tools each own a DIFFERENT rep range (phase-1 "
                         "tool owns 1..15, phase-2 tool owns 16..30) -- "
                         "checking one tool across the full 1..30 range "
                         "will always report the other tool's half as "
                         "missing.")
    ap.add_argument("--reps", type=int, default=30)
    ap.add_argument("--overhead-reps", type=int, default=10)
    ap.add_argument("--stages", default="baseline,idle,fault",
                    help="overhead mode: comma list from baseline,idle,fault")
    ap.add_argument("--strategies", default="random,coverage",
                    help="campaign mode: comma list of strategy arm names")
    ap.add_argument("--campaigns", type=int, default=10)
    ap.add_argument("--injections", type=int, default=10)
    ap.add_argument("--no-sidecar", action="store_true")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    problems: list = []
    expected = 0

    if args.mode == "benchmark":
        if not args.tool:
            ap.error("--tool is required for benchmark mode")
        for scenario in SCENARIOS:
            for rep in range(args.start_rep, args.reps + 1):
                expected += 1
                check_run(data_dir / args.tool / scenario / f"run-{rep}.json",
                          problems, args.no_sidecar)
    elif args.mode == "overhead":
        if not args.tool:
            ap.error("--tool is required for overhead mode")
        for stage in args.stages.split(","):
            label = "baseline" if stage == "baseline" else f"{args.tool}-{stage}"
            for rep in range(1, args.overhead_reps + 1):
                expected += 1
                check_run(data_dir / "overhead" / label / f"run-{rep}.json",
                          problems, args.no_sidecar)
    else:  # campaign
        for strategy in args.strategies.split(","):
            for camp in range(1, args.campaigns + 1):
                cdir = data_dir / "campaigns" / strategy / f"campaign-{camp}"
                for inj in range(1, args.injections + 1):
                    expected += 1
                    check_run(cdir / f"injection-{inj}.json", problems, args.no_sidecar)
                if not (cdir / "campaign-summary.json").exists():
                    problems.append(f"MISSING  {cdir / 'campaign-summary.json'}")

    ok = expected - sum(1 for p in problems if p.startswith(("MISSING", "CORRUPT")))
    print(f"expected runs: {expected}  present+parsed: {ok}  problems: {len(problems)}")
    for p in problems:
        print(f"  {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
