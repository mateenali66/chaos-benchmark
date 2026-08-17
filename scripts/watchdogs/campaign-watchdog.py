#!/usr/bin/env -S python3 -u
"""
Campaign Watchdog (Component 3: 5 arms x 10 campaigns x 10 injections = 500
injections, run-all-campaigns.sh across 3 slots on is-chaos-ml).

Unlike experiment-watchdog.py (which parses a shared progress.log's PASS/FAIL
line grammar), campaign progress lands as JSON files directly
(data-v2/ml/campaigns/{arm}/campaign-{N}/injection-{i}.json), so this
watchdog polls the filesystem instead of parsing logs: total injection-file
count across all 50 campaigns, and each slot driver's own PID liveness via
--pidfile (one per slot, comma-separated).

Flags:
  - no new injection-*.json file within --stall-minutes (default 20) while
    fewer than 500 total exist
  - any slot's pidfile process is dead while that slot still has incomplete
    campaigns assigned to it (same flat-index-%3 assignment
    run-all-campaigns.sh uses, recomputed here so this watchdog needs no
    extra bookkeeping file)
  - any injection-*.json with a non-null "error" field (chaoslib.run_fault_protocol
    catches per-injection exceptions and records them without stopping the
    campaign driver -- file-count progress alone would look "healthy" even
    if every single injection were failing, since a caught exception still
    produces a file). Alerts on any NEW error count since the previous poll,
    not just presence, so this only fires once per newly-failed injection.
  - cluster resource constraints: any of the 3 is-chaos-ml nodes reporting
    NotReady or a True DiskPressure/MemoryPressure/PIDPressure condition, or
    any pod in social-network/social-network-1/social-network-2 stuck
    Pending for more than 3 minutes (the actual signature of the CPU-capacity
    deadlock hit live 2026-08-16 setting this campaign up -- nodes stayed
    "Ready" the whole time, only pod scheduling failed, so a node-Ready-only
    check would have missed it)
  - any campaign-summary.json reporting a violation_counts total of 0 across
    an entire arm's 10 campaigns is NOT flagged here (that's an analysis
    question, not an infra failure) -- this watchdog is infra-health only

Writes one JSON object per line (JSONL) to --status-file on every poll, and
touches "<status-file>.ALERT" (create-or-update mtime) on any alert
condition, matching experiment-watchdog.py's convention. Never exits on its
own.

Usage:
  python3 -u campaign-watchdog.py \
      --data-dir data-v2/ml/campaigns \
      --status-file data-v2/ml/watchdogs/campaign-status.jsonl \
      --pidfiles data-v2/ml/watchdogs/slot0.pid,data-v2/ml/watchdogs/slot1.pid,data-v2/ml/watchdogs/slot2.pid \
      --stall-minutes 20
"""
import argparse
import calendar
import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

os.environ.setdefault("PYTHONUNBUFFERED", "1")
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except AttributeError:
    pass

ARMS = ["random", "coverage", "llm-claude", "llm-llama", "llm-mistral"]
CAMPAIGNS_PER_ARM = 10
CAMPAIGN_K = 10
TOTAL_INJECTIONS = len(ARMS) * CAMPAIGNS_PER_ARM * CAMPAIGN_K  # 500


def slot_for(arm_idx: int, campaign: int) -> int:
    """Mirrors run-all-campaigns.sh's flat-index-%3 assignment exactly."""
    idx = arm_idx * CAMPAIGNS_PER_ARM + (campaign - 1)
    return idx % 3


def pid_alive(pidfile: str) -> bool:
    if not pidfile or not os.path.exists(pidfile):
        return False
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip())
    except (ValueError, OSError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone/something else
    except OSError:
        return False
    return True


def scan_injection_errors(data_dir: str) -> list[str]:
    """Every injection-*.json with a non-null "error" field, as
    "arm/campaign-N/injection-i" identifiers. Full rescan each poll (500
    files max, cheap at a 60s interval) rather than tracking deltas by
    mtime, so a watchdog restart never misses an error that landed while it
    was down."""
    errored = []
    for path in glob.glob(os.path.join(data_dir, "*", "campaign-*", "injection-*.json")):
        try:
            with open(path) as f:
                d = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if d.get("error"):
            parts = path.split(os.sep)
            errored.append("/".join(parts[-3:]))
    return sorted(errored)


def _run_kubectl(args: list[str], context: str, timeout: int = 20) -> dict | list | None:
    try:
        result = subprocess.run(
            ["kubectl", "--context", context] + args + ["-o", "json"],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def check_cluster_health(context: str, namespaces: list[str],
                          pending_grace_s: int = 180) -> dict:
    """Resource-constraint proxy: node conditions (NotReady, *Pressure) plus
    pods stuck Pending past `pending_grace_s`. `None` fields mean the check
    itself could not run (kubectl unreachable/timeout), NOT that the cluster
    is healthy -- reported as its own flag so a kubectl outage doesn't read
    as a silent all-clear."""
    unhealthy_nodes = []
    nodes = _run_kubectl(["get", "nodes"], context)
    if nodes is None:
        return {"checked": False, "unhealthy_nodes": None, "pending_pods": None}
    for node in nodes.get("items", []):
        name = node["metadata"]["name"]
        conditions = {c["type"]: c["status"] for c in node.get("status", {}).get("conditions", [])}
        if conditions.get("Ready") != "True":
            unhealthy_nodes.append(f"{name}: NotReady")
            continue
        for pressure in ("DiskPressure", "MemoryPressure", "PIDPressure"):
            if conditions.get(pressure) == "True":
                unhealthy_nodes.append(f"{name}: {pressure}")

    now = time.time()
    pending_pods = []
    for ns in namespaces:
        pods = _run_kubectl(["get", "pods", "-n", ns], context)
        if pods is None:
            continue
        for pod in pods.get("items", []):
            if pod.get("status", {}).get("phase") != "Pending":
                continue
            created = pod["metadata"].get("creationTimestamp")
            if not created:
                continue
            created_ts = calendar.timegm(time.strptime(created, "%Y-%m-%dT%H:%M:%SZ"))
            if now - created_ts > pending_grace_s:
                pending_pods.append(f"{ns}/{pod['metadata']['name']}")

    return {"checked": True, "unhealthy_nodes": unhealthy_nodes, "pending_pods": pending_pods}


def scan(data_dir: str) -> dict:
    injection_files = glob.glob(os.path.join(data_dir, "*", "campaign-*", "injection-*.json"))
    latest_mtime = max((os.path.getmtime(f) for f in injection_files), default=0.0)

    incomplete_slots = set()
    campaigns_done = 0
    for arm_idx, arm in enumerate(ARMS):
        for campaign in range(1, CAMPAIGNS_PER_ARM + 1):
            summary = os.path.join(data_dir, arm, f"campaign-{campaign}", "campaign-summary.json")
            done = False
            if os.path.exists(summary):
                try:
                    with open(summary) as f:
                        d = json.load(f)
                    done = d.get("injections_completed", 0) >= CAMPAIGN_K
                except (json.JSONDecodeError, OSError):
                    pass
            if done:
                campaigns_done += 1
            else:
                incomplete_slots.add(slot_for(arm_idx, campaign))

    return {
        "injection_count": len(injection_files),
        "total_injections": TOTAL_INJECTIONS,
        "campaigns_done": campaigns_done,
        "total_campaigns": len(ARMS) * CAMPAIGNS_PER_ARM,
        "latest_mtime": latest_mtime,
        "incomplete_slots": sorted(incomplete_slots),
    }


def main():
    parser = argparse.ArgumentParser(description="Component 3 campaign watchdog")
    parser.add_argument("--data-dir", required=True, help="path to data-v2/ml/campaigns")
    parser.add_argument("--status-file", required=True, help="path to JSONL status output")
    parser.add_argument("--pidfiles", default="", help="comma-separated pidfiles, index = slot number")
    parser.add_argument("--stall-minutes", type=float, default=20.0,
                         help="alert if no new injection-*.json in this many minutes (default: 20)")
    parser.add_argument("--interval", type=int, default=60, help="poll interval in seconds (default: 60)")
    parser.add_argument("--kube-context", default="is-chaos-ml",
                         help="kubectl context for cluster-health checks (default: is-chaos-ml)")
    parser.add_argument("--namespaces", default="social-network,social-network-1,social-network-2",
                         help="comma-separated namespaces to check for stuck-Pending pods")
    parser.add_argument("--cluster-check-interval", type=int, default=300,
                         help="seconds between cluster-health checks (default: 300; kubectl calls "
                              "are heavier than the filesystem scan, so this runs less often than --interval)")
    args = parser.parse_args()
    namespaces = args.namespaces.split(",")

    status_file = args.status_file
    alert_sentinel = status_file + ".ALERT"
    status_dir = os.path.dirname(os.path.abspath(status_file))
    if status_dir:
        os.makedirs(status_dir, exist_ok=True)

    pidfiles = args.pidfiles.split(",") if args.pidfiles else []

    print(f"[campaign-watchdog] data_dir={args.data_dir} stall_minutes={args.stall_minutes} "
          f"pidfiles={pidfiles} status_file={status_file}", flush=True)

    last_injection_count = None
    last_progress_wallclock = time.time()
    known_errors: set[str] = set()
    last_cluster_check = 0.0
    last_cluster_health = {"checked": False, "unhealthy_nodes": None, "pending_pods": None}

    while True:
        state = scan(args.data_dir)
        now = time.time()

        if last_injection_count is None or state["injection_count"] > last_injection_count:
            last_progress_wallclock = now
        last_injection_count = state["injection_count"]

        stalled = (
            state["injection_count"] < state["total_injections"]
            and (now - last_progress_wallclock) > args.stall_minutes * 60
        )

        dead_slots = []
        for slot_num, pidfile in enumerate(pidfiles):
            if slot_num in state["incomplete_slots"] and not pid_alive(pidfile):
                dead_slots.append(slot_num)

        errored_injections = set(scan_injection_errors(args.data_dir))
        newly_errored = sorted(errored_injections - known_errors)
        new_errors = len(newly_errored)
        known_errors = errored_injections

        if now - last_cluster_check >= args.cluster_check_interval:
            last_cluster_health = check_cluster_health(args.kube_context, namespaces)
            last_cluster_check = now
        cluster_health = last_cluster_health

        alert = (
            stalled or bool(dead_slots) or new_errors > 0
            or bool(cluster_health.get("unhealthy_nodes"))
            or bool(cluster_health.get("pending_pods"))
        )

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "injections_done": state["injection_count"],
            "injections_total": state["total_injections"],
            "campaigns_done": state["campaigns_done"],
            "campaigns_total": state["total_campaigns"],
            "incomplete_slots": state["incomplete_slots"],
            "dead_slots": dead_slots,
            "stalled": stalled,
            "minutes_since_progress": round((now - last_progress_wallclock) / 60, 1),
            "total_errored_injections": len(known_errors),
            "new_errored_injections": newly_errored,
            "cluster_health": cluster_health,
            "alert": alert,
        }

        with open(status_file, "a") as f:
            f.write(json.dumps(record) + "\n")

        if alert:
            os.utime(alert_sentinel, None) if os.path.exists(alert_sentinel) else open(alert_sentinel, "w").close()
            reasons = []
            if stalled:
                reasons.append(f"no new injection in {record['minutes_since_progress']}min")
            if dead_slots:
                reasons.append(f"dead slot(s): {dead_slots}")
            if new_errors > 0:
                reasons.append(f"{new_errors} new injection error(s): {record['new_errored_injections']}")
            if cluster_health.get("unhealthy_nodes"):
                reasons.append(f"unhealthy node(s): {cluster_health['unhealthy_nodes']}")
            if cluster_health.get("pending_pods"):
                reasons.append(f"stuck Pending pod(s): {cluster_health['pending_pods']}")
            print(f"[campaign-watchdog] ALERT: {'; '.join(reasons)} "
                  f"({state['injection_count']}/{state['total_injections']} injections)", file=sys.stderr)
        else:
            print(f"[campaign-watchdog] OK: {state['injection_count']}/{state['total_injections']} injections, "
                  f"{state['campaigns_done']}/{state['total_campaigns']} campaigns done, "
                  f"{len(known_errors)} total errored injections, "
                  f"cluster_checked={cluster_health['checked']}")

        if state["injection_count"] >= state["total_injections"] and not state["incomplete_slots"]:
            print("[campaign-watchdog] All campaigns complete. Exiting.")
            break

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
