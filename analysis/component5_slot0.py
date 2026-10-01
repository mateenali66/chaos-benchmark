#!/usr/bin/env python3
"""Component 5 restricted to the runs whose sidecars hold their own telemetry.

Component 1 ran three slots of the application at once (scripts/SLOT_PARALLELISM.md).
The sidecar writer always queried the slot-0 namespace, so the sidecars of
slot-1 and slot-2 runs hold slot 0's telemetry from a concurrent run with a
different fault. Only slot-0 runs (scenarios p1, p2, p3, n1; 240 runs) have
their own telemetry. See the 2026-09-30 erratum in analysis/PREREGISTRATION.md.

Detectors are trained and scored per run, so the per-run AUCs of slot-0 runs in
analysis/results/component5-scoring.json are valid as computed. This script
filters them and recomputes, with the same code as the published analysis:
the per-scorer summary and confirmatory test (scripts/component5_evaluate.py),
and the two post hoc analyses (analysis/component5_sensitivity.py).

Usage: python analysis/component5_slot0.py [--in PATH] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "scripts"))
sys.path.insert(0, str(ROOT))

from component5_evaluate import summarize  # noqa: E402
from component5_sensitivity import degenerate_split, sensitivity  # noqa: E402

SLOT0_SCENARIOS = ("p1", "p2", "p3", "n1")


def main() -> None:
    ap = argparse.ArgumentParser(description="Component 5 on slot-0 runs only")
    ap.add_argument("--in", dest="inp", type=Path, default=ROOT / "results" / "component5-scoring.json")
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "component5-slot0.json")
    args = ap.parse_args()

    data = json.loads(args.inp.read_text())
    runs = [r for r in data["per_run"] if r["metadata"]["scenario"] in SLOT0_SCENARIOS]
    summary, confirmatory = summarize(runs)
    result = {
        "scenarios": list(SLOT0_SCENARIOS),
        "n_runs_scored": len(runs),
        "summary": summary,
        "confirmatory_vs_static_threshold": confirmatory,
        "post_hoc_sensitivity_degenerate_as_0_5": sensitivity(runs),
        "post_hoc_detector_medians_by_baseline_degeneracy": degenerate_split(runs),
    }
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(f"wrote {args.out} ({len(runs)} runs)")


if __name__ == "__main__":
    main()
