#!/usr/bin/env -S python3 -u
"""One-shot: compute Component 4's steady-state baseline summary from real
Component 2 data (data-v2/bench-*/overhead/baseline/*) and write it to
experiments/component4-baseline-summary.json.

Component 2's baseline config is the only genuinely no-tool, no-fault,
steady-state data in this study (Component 1's benchmark-mode runs mix
baseline+fault+recovery into one wrk2 measurement window, so they are NOT
a clean baseline source for this purpose).

Instrumentation honesty note: wrk2 measures whole-app latency through the
single nginx-thrift entry point; there is no per-service latency
breakdown in this testbed. Component 4's hypothesis prompt asks about
per-service p99 degradation, so the per-service signal actually supplied
is CPU utilization (from the timeseries sidecars), not latency -- disclosed
in both this script's output and the prompt addendum in hypothesis.txt.

Run once after Component 1/2 data collection; re-run only if Component 2
data changes (it won't -- it's a frozen, verified-complete dataset).
"""
import glob
import gzip
import json
import statistics as st
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = PROJECT_ROOT / "experiments" / "component4-baseline-summary.json"


def pod_to_service(pod: str) -> str:
    parts = pod.split("-")
    return "-".join(parts[:-2]) if len(parts) >= 3 else pod


def main() -> int:
    thr, p50s, p99s = [], [], []
    for f in glob.glob(str(PROJECT_ROOT / "data-v2" / "bench-*" / "overhead" / "baseline" / "run-*.json")):
        d = json.loads(Path(f).read_text())
        w = d.get("wrk2", {})
        if w.get("throughput_rps"):
            thr.append(w["throughput_rps"])
        lm = w.get("latency_ms", {})
        if lm.get("p50"):
            p50s.append(lm["p50"])
        if lm.get("p99"):
            p99s.append(lm["p99"])

    per_svc_cpu = defaultdict(list)
    for f in glob.glob(str(PROJECT_ROOT / "data-v2" / "bench-*" / "overhead" / "baseline" / "run-*.timeseries.json.gz")):
        with gzip.open(f, "rt") as fh:
            d = json.load(fh)
        cpu = d.get("metrics", {}).get("container_cpu_usage", {})
        for series in cpu.get("result", []):
            pod = series.get("metric", {}).get("pod", "")
            svc = pod_to_service(pod)
            if svc.startswith("wrk2"):
                continue  # load generator's own pod, not an app service
            vals = [float(v[1]) for v in series.get("values", []) if v[1] not in ("NaN", "")]
            if vals:
                per_svc_cpu[svc].append(sum(vals) / len(vals))

    if not thr:
        print("ERROR: no Component 2 baseline data found under data-v2/bench-*/overhead/baseline/", file=sys.stderr)
        return 1

    summary = {
        "source": "Component 2 overhead baseline config (no tool, no fault, 120 rps, "
                  "n=20 across bench-a+bench-b) -- the only true steady-state data in this study",
        "sample_size": len(thr),
        "instrumentation_note": (
            "wrk2 measures whole-app latency through the single nginx-thrift entry "
            "point; there is no per-service latency instrumentation in this testbed. "
            "The per-service signal below is mean CPU utilization (cores), not latency."
        ),
        "aggregate_throughput_rps": round(st.median(thr), 1),
        "aggregate_p50_latency_ms": round(st.median(p50s), 1),
        "aggregate_p99_latency_ms": round(st.median(p99s), 1),
        "per_service_mean_cpu_cores": {
            svc: round(st.mean(vals), 4) for svc, vals in sorted(per_svc_cpu.items())
        },
    }
    OUTPUT_FILE.write_text(json.dumps(summary, indent=2) + "\n")
    print(f"wrote {OUTPUT_FILE} ({len(thr)} throughput samples, {len(per_svc_cpu)} services)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
