#!/usr/bin/env -S python3 -u
"""Regenerate timeseries sidecars whose range queries failed.

The initial revision-campaign runs wrote sidecars after the Prometheus
port-forward was torn down, so every metric recorded a connection error.
The window/fault timestamps in the sidecar metadata are correct, and
Prometheus retains the underlying data (kube-prometheus default retention),
so the series can be re-queried as long as this runs within the retention
window.

Usage (with the target cluster's env active: KUBECONFIG + CHAOS_PROM_PORT):
  backfill-sidecars.py --data-dir data-v2/bench-a [--dry-run]

Rewrites only sidecars where every metric is an error; healthy sidecars are
left untouched. Prints a per-file verdict and a final summary.
"""

import argparse
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chaoslib  # noqa: E402


def all_errors(metrics: dict) -> bool:
    if not metrics:
        return True
    return all(isinstance(v, dict) and "error" in v for v in metrics.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="regenerate ALL sidecars, not only broken ones (e.g. after a query fix)")
    args = ap.parse_args()

    # Iterate RUN JSONs, not sidecars: a run whose sidecar was never written
    # at all must show up here too (bench-b run-3 lesson).
    todo_missing: list = []
    run_jsons = [p for p in sorted(Path(args.data_dir).rglob("run-*.json"))
                 if ".timeseries." not in p.name]
    run_jsons += [p for p in sorted(Path(args.data_dir).rglob("injection-*.json"))
                  if ".timeseries." not in p.name]
    sidecars = []
    for rj in run_jsons:
        sc = rj.parent / f"{rj.stem}.timeseries.json.gz"
        if sc.exists():
            sidecars.append(sc)
            continue
        # Missing sidecar: derive the window from the run JSON's phase stamps.
        try:
            run_data = json.loads(rj.read_text())
            phases = run_data.get("phases", {})
            starts = [v["start"] for v in phases.values() if isinstance(v, dict) and "start" in v]
            ends = [v["end"] for v in phases.values() if isinstance(v, dict) and "end" in v]
            meta = {
                "window_start": float(min(starts)) - 60,
                "window_end": float(max(ends)),
                "fault_start": (phases.get("fault") or {}).get("start"),
                "fault_end": (phases.get("fault") or {}).get("end"),
            }
            print(f"NOSIDECAR  {sc.name}: will regenerate from run JSON phases")
            todo_missing.append((sc, meta))
        except Exception as exc:
            print(f"NOSIDECAR  {sc.name}: cannot derive window: {exc}")
            todo_missing.append((sc, None))
    if not sidecars and not todo_missing:
        print("no sidecars found")
        return 0

    todo = list(todo_missing)
    for path in sidecars:
        try:
            with gzip.open(path, "rt") as fh:
                payload = json.load(fh)
        except Exception as exc:
            print(f"UNREADABLE {path}: {exc}")
            todo.append((path, None))
            continue
        if args.force or all_errors(payload.get("metrics", {})):
            todo.append((path, payload.get("metadata")))
        else:
            print(f"HEALTHY    {path.name}")

    print(f"\n{len(todo)} of {len(sidecars)} sidecars need backfill")
    if args.dry_run or not todo:
        return 0

    chaoslib.start_port_forward("monitoring", "prometheus-kube-prometheus-prometheus",
                                chaoslib.PROMETHEUS_PORT, 9090)
    failed = 0
    try:
        for path, meta in todo:
            if not meta or meta.get("window_start") is None:
                print(f"SKIP       {path.name}: no usable metadata")
                failed += 1
                continue
            result = chaoslib.write_timeseries_sidecar(
                # write_timeseries_sidecar derives the sidecar name from the
                # run JSON path, so hand it the matching run file
                path.parent / path.name.replace(".timeseries.json.gz", ".json"),
                meta["window_start"], meta["window_end"],
                meta.get("fault_start"), meta.get("fault_end"),
                prom_available=True,
                namespace=meta.get("namespace", chaoslib.NAMESPACE),
                step=meta.get("step", chaoslib.TIMESERIES_STEP),
            )
            if result is None:
                print(f"FAILED     {path.name}")
                failed += 1
            else:
                with gzip.open(result, "rt") as fh:
                    fresh = json.load(fh)
                verdict = "STILL-BAD" if all_errors(fresh.get("metrics", {})) else "BACKFILLED"
                if verdict == "STILL-BAD":
                    failed += 1
                print(f"{verdict} {path.name}")
    finally:
        chaoslib.cleanup_port_forwards()

    print(f"\nbackfill complete: {len(todo) - failed} fixed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
