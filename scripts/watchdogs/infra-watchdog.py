#!/usr/bin/env -S python3 -u
"""
Infra Watchdog
Polls a single EKS cluster (by kubeconfig context) every --interval seconds
and flags conditions that would silently corrupt a chaos-benchmark campaign:

  - fewer than --expected-nodes nodes Ready, or any NotReady nodes
  - pods stuck Pending for longer than --pending-minutes in the app +
    chaos-tooling namespaces
  - OOMKilled containers / Evicted pods in kube-system
  - PVCs stuck off Bound for longer than --pvc-pending-minutes

Writes one JSON object per line (JSONL) to --status-file on every poll,
regardless of health. On any alert condition it ALSO touches a sentinel file
at "<status-file>.ALERT" (create-or-update mtime) so an outer monitor can
`test -f`/watch mtime without parsing JSON, then keeps looping -- this
watchdog never exits on its own.

Usage:
  python3 -u infra-watchdog.py is-chaos-bench-a \
      --status-file data/watchdogs/bench-a/infra-status.jsonl

Runs unbuffered (shebang: `env -S python3 -u`; stdout/stderr also
reconfigured to line-buffered below as a belt-and-suspenders fallback for
invocations that strip shebang args, e.g. `python3 infra-watchdog.py`).
"""
import argparse
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
    pass  # Python < 3.7 fallback; not expected in this environment


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def kubectl_json(context, args, timeout=30):
    """Run `kubectl --context <context> <args> -o json` and return parsed JSON,
    or None on any error (missing binary, bad context, API timeout, etc.)."""
    cmd = ["kubectl", "--context", context] + args + ["-o", "json"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return None, f"{' '.join(cmd)} failed: {exc}"
    if result.returncode != 0:
        return None, f"{' '.join(cmd)} exit {result.returncode}: {result.stderr.strip()[:300]}"
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"{' '.join(cmd)} bad JSON: {exc}"


def parse_k8s_ts(ts):
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def age_minutes(ts):
    dt = parse_k8s_ts(ts)
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0


def check_nodes(context, expected_nodes):
    data, err = kubectl_json(context, ["get", "nodes"])
    if err:
        return {"error": err}, [f"could not list nodes: {err}"]

    ready = []
    not_ready = []
    for item in data.get("items", []):
        name = item.get("metadata", {}).get("name", "<unknown>")
        conditions = item.get("status", {}).get("conditions", [])
        ready_cond = next((c for c in conditions if c.get("type") == "Ready"), None)
        if ready_cond and ready_cond.get("status") == "True":
            ready.append(name)
        else:
            not_ready.append(name)

    alerts = []
    if len(ready) < expected_nodes:
        alerts.append(
            f"only {len(ready)}/{expected_nodes} nodes Ready (not-ready: {not_ready})"
        )
    if not_ready:
        alerts.append(f"NotReady nodes: {not_ready}")

    return {
        "ready_count": len(ready),
        "expected": expected_nodes,
        "ready_nodes": ready,
        "not_ready_nodes": not_ready,
    }, alerts


def check_pending_pods(context, namespaces, pending_minutes):
    stuck = []
    errors = []
    for ns in namespaces:
        data, err = kubectl_json(context, ["get", "pods", "-n", ns])
        if err:
            errors.append(f"ns={ns}: {err}")
            continue
        for item in data.get("items", []):
            status = item.get("status", {})
            if status.get("phase") != "Pending":
                continue
            created = item.get("metadata", {}).get("creationTimestamp")
            age = age_minutes(created)
            if age is not None and age >= pending_minutes:
                stuck.append(
                    {
                        "namespace": ns,
                        "pod": item.get("metadata", {}).get("name"),
                        "age_minutes": round(age, 1),
                    }
                )

    alerts = []
    if stuck:
        alerts.append(f"{len(stuck)} pod(s) Pending >= {pending_minutes}m: {stuck}")
    if errors:
        alerts.append(f"pending-pod check errors: {errors}")
    return {"stuck_pending": stuck, "check_errors": errors}, alerts


def check_kube_system_health(context):
    data, err = kubectl_json(context, ["get", "pods", "-n", "kube-system"])
    if err:
        return {"error": err}, [f"could not list kube-system pods: {err}"]

    oom_or_evicted = []
    for item in data.get("items", []):
        meta = item.get("metadata", {})
        pod_name = meta.get("name")
        status = item.get("status", {})

        if status.get("reason") == "Evicted":
            oom_or_evicted.append(
                {"pod": pod_name, "reason": "Evicted", "message": status.get("message", "")[:200]}
            )
            continue

        for cs in status.get("containerStatuses", []) or []:
            for state_key in ("state", "lastState"):
                terminated = (cs.get(state_key) or {}).get("terminated")
                if terminated and terminated.get("reason") == "OOMKilled":
                    oom_or_evicted.append(
                        {
                            "pod": pod_name,
                            "container": cs.get("name"),
                            "reason": "OOMKilled",
                            "restart_count": cs.get("restartCount"),
                            "seen_in": state_key,
                        }
                    )

    alerts = []
    if oom_or_evicted:
        alerts.append(f"{len(oom_or_evicted)} OOMKilled/Evicted event(s) in kube-system: {oom_or_evicted}")
    return {"oom_or_evicted": oom_or_evicted}, alerts


def check_pvc_binding(context, pvc_pending_minutes):
    data, err = kubectl_json(context, ["get", "pvc", "-A"])
    if err:
        return {"error": err}, [f"could not list PVCs: {err}"]

    stuck = []
    for item in data.get("items", []):
        status = item.get("status", {})
        phase = status.get("phase")
        if phase == "Bound":
            continue
        created = item.get("metadata", {}).get("creationTimestamp")
        age = age_minutes(created)
        if age is not None and age >= pvc_pending_minutes:
            stuck.append(
                {
                    "namespace": item.get("metadata", {}).get("namespace"),
                    "pvc": item.get("metadata", {}).get("name"),
                    "phase": phase,
                    "age_minutes": round(age, 1),
                }
            )

    alerts = []
    if stuck:
        alerts.append(
            f"{len(stuck)} PVC(s) not Bound after {pvc_pending_minutes}m: {stuck}"
        )
    return {"stuck_pvcs": stuck}, alerts


def touch(path):
    with open(path, "a"):
        pass
    os.utime(path, None)


def main():
    parser = argparse.ArgumentParser(description="Infra watchdog for a single chaos-benchmark EKS cluster")
    parser.add_argument("context", help="kubeconfig context name, e.g. is-chaos-bench-a")
    parser.add_argument("--status-file", required=True, help="path to JSONL status output")
    parser.add_argument("--interval", type=int, default=60, help="poll interval in seconds (default: 60)")
    parser.add_argument("--expected-nodes", type=int, default=3, help="expected Ready node count (default: 3)")
    parser.add_argument(
        "--namespaces",
        default="social-network,chaos-testing,litmus",
        help="comma-separated app + chaos namespaces to check for stuck Pending pods",
    )
    parser.add_argument("--pending-minutes", type=float, default=5.0, help="pod Pending age threshold in minutes (default: 5)")
    parser.add_argument("--pvc-pending-minutes", type=float, default=5.0, help="PVC non-Bound age threshold in minutes (default: 5)")
    args = parser.parse_args()

    namespaces = [n.strip() for n in args.namespaces.split(",") if n.strip()]
    status_file = args.status_file
    alert_sentinel = status_file + ".ALERT"

    status_dir = os.path.dirname(os.path.abspath(status_file))
    if status_dir:
        os.makedirs(status_dir, exist_ok=True)

    print(f"[infra-watchdog] context={args.context} interval={args.interval}s "
          f"expected_nodes={args.expected_nodes} namespaces={namespaces} "
          f"status_file={status_file}", flush=True)

    while True:
        loop_start = time.time()
        alerts = []

        nodes_report, nodes_alerts = check_nodes(args.context, args.expected_nodes)
        alerts += nodes_alerts

        pending_report, pending_alerts = check_pending_pods(args.context, namespaces, args.pending_minutes)
        alerts += pending_alerts

        kube_system_report, kube_system_alerts = check_kube_system_health(args.context)
        alerts += kube_system_alerts

        pvc_report, pvc_alerts = check_pvc_binding(args.context, args.pvc_pending_minutes)
        alerts += pvc_alerts

        level = "ALERT" if alerts else "OK"
        record = {
            "ts": now_iso(),
            "watchdog": "infra",
            "context": args.context,
            "level": level,
            "nodes": nodes_report,
            "pending_pods": pending_report,
            "kube_system": kube_system_report,
            "pvcs": pvc_report,
            "alerts": alerts,
        }

        with open(status_file, "a") as f:
            f.write(json.dumps(record) + "\n")

        if level == "ALERT":
            touch(alert_sentinel)
            print(f"[infra-watchdog] {record['ts']} ALERT ({len(alerts)}): {alerts}", flush=True)
        else:
            print(f"[infra-watchdog] {record['ts']} OK "
                  f"(nodes {nodes_report.get('ready_count', '?')}/{args.expected_nodes})", flush=True)

        elapsed = time.time() - loop_start
        time.sleep(max(0.0, args.interval - elapsed))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
