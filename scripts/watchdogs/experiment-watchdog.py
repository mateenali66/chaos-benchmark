#!/usr/bin/env -S python3 -u
"""
Experiment Watchdog
Watches a chaos-benchmark campaign's progress log (data/progress.log format,
written by scripts/run-all-experiments.sh) and flags:

  - no new completed run (a "  PASS: tool / scenario / run N" line) within
    --stall-minutes (default: 25)
  - any run that reports "  FAIL: tool / scenario / run N"
  - the runner process (given via --pidfile) is dead while runs remain
    (parsed from the most recent "[current/total]" progress marker, or the
    "Batch Complete" trailer if the run finished)

Writes one JSON object per line (JSONL) to --status-file on every poll,
regardless of health. On any alert condition it ALSO touches a sentinel file
at "<status-file>.ALERT" (create-or-update mtime) so an outer monitor can
`test -f`/watch mtime without parsing JSON, then keeps looping -- this
watchdog never exits on its own.

Usage:
  python3 -u experiment-watchdog.py data/progress.log \
      --status-file data/watchdogs/bench-a/experiment-status.jsonl \
      --pidfile data/watchdogs/bench-a/run-all.pid \
      --stall-minutes 25

The pidfile is written by whoever launches run-all-experiments.sh, e.g.:
  nohup ./scripts/run-all-experiments.sh > data/run-all.log 2>&1 &
  echo $! > data/watchdogs/bench-a/run-all.pid

Runs unbuffered (shebang: `env -S python3 -u`; stdout/stderr also
reconfigured to line-buffered below as a belt-and-suspenders fallback for
invocations that strip shebang args, e.g. `python3 experiment-watchdog.py`).
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

os.environ.setdefault("PYTHONUNBUFFERED", "1")
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except AttributeError:
    pass  # Python < 3.7 fallback; not expected in this environment

# Two log grammars share progress.log:
#   benchmark (run-all-experiments.sh):  "  PASS: tool / scenario / run N"
#                                        "[current/total] RUN|SKIP"
#   overhead/campaign (run-overhead.sh): "[idle 3/10] PASS" / "[baseline 1/10] RUN  label"
#
# Slot parallelism (run-all-experiments.sh --slot N, N!=0) prepends a literal
# "[slot N] " to EVERY benchmark-grammar line it emits (PASS, FAIL, the
# [current/total] marker, and Batch Complete) -- confirmed against
# run-all-experiments.sh's LOG_PREFIX usage, applied to all of its emitted
# lines. The original anchors here (^\s*) cannot skip over that literal
# prefix text, so every regex below tolerates an optional leading
# "[slot N] " before the benchmark-grammar branch. run-overhead.sh has no
# slot support and never emits this prefix, so the overhead-grammar branch
# is left as-is.
_SLOT_PREFIX = r"(?:\[slot \d+\]\s*)?"
PASS_RE = re.compile(
    rf"^{_SLOT_PREFIX}\s*PASS:\s*(\S+)\s*/\s*(\S+)\s*/\s*run\s*(\d+)"
    r"|^\[(baseline|idle|fault|injection)\s+(\d+)/(\d+)\]\s+PASS\b",
    re.MULTILINE)
FAIL_RE = re.compile(
    rf"^{_SLOT_PREFIX}\s*FAIL:\s*(\S+)\s*/\s*(\S+)\s*/\s*run\s*(\d+)"
    r"|^\[(baseline|idle|fault|injection)\s+(\d+)/(\d+)\]\s+FAIL\b",
    re.MULTILINE)
PROGRESS_MARKER_RE = re.compile(
    rf"^{_SLOT_PREFIX}\[(\d+)/(\d+)\]\s+(RUN|SKIP)\b"
    r"|^\[(?:baseline|idle|fault|injection)\s+(\d+)/(\d+)\]\s+(RUN|SKIP)\b",
    re.MULTILINE)
BATCH_COMPLETE_RE = re.compile(
    rf"^{_SLOT_PREFIX}\s*Batch Complete\s*$"
    r"|^\s*Overhead batch .* complete", re.MULTILINE)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def touch(path):
    with open(path, "a"):
        pass
    os.utime(path, None)


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone/something else
    except OSError:
        return False
    return True


def read_log(path):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return None


def analyze_log(text):
    """Parse the whole progress log (cheap for this project's scale: a full
    120-run campaign is a few thousand lines) and return counts + the latest
    progress marker."""
    passes = PASS_RE.findall(text)
    fails = FAIL_RE.findall(text)

    # Alternation fills either groups (1,2,3) [benchmark grammar] or the
    # overhead-grammar groups; normalize each findall tuple to (a, b, c).
    def _norm(tup):
        first = tuple(g for g in tup[:3] if g)
        return first if len(first) == 3 else tuple(g for g in tup[3:] if g)

    fails = [_norm(t) for t in fails]

    markers = PROGRESS_MARKER_RE.findall(text)
    if markers:
        m = _norm(markers[-1])
        current, total = int(m[0]), int(m[1])
    else:
        current, total = 0, 0

    # Positional: multiple stages append to one log, so "complete" only counts
    # if nothing has STARTED after the last completion line (else stage (a)'s
    # completion would suppress stall detection for stages (b)/(c) forever).
    complete_matches = list(BATCH_COMPLETE_RE.finditer(text))
    marker_matches = list(PROGRESS_MARKER_RE.finditer(text))
    batch_complete = bool(complete_matches) and (
        not marker_matches or marker_matches[-1].start() < complete_matches[-1].start()
    )
    if batch_complete:
        remaining = 0
    else:
        remaining = max(0, total - current)

    return {
        "pass_count": len(passes),
        "fail_count": len(fails),
        "fails": [f"{tool}/{scenario}/run-{run}" for tool, scenario, run in fails],
        "current": current,
        "total": total,
        "remaining": remaining,
        "batch_complete": batch_complete,
    }


def main():
    parser = argparse.ArgumentParser(description="Experiment (campaign progress) watchdog")
    parser.add_argument("progress_log", help="path to data/progress.log")
    parser.add_argument("--status-file", required=True, help="path to JSONL status output")
    parser.add_argument("--pidfile", default=None, help="path to a file containing the run-all-experiments.sh PID")
    parser.add_argument("--stall-minutes", type=float, default=25.0, help="alert if no new completed (PASS) run in this many minutes (default: 25)")
    parser.add_argument("--interval", type=int, default=60, help="poll interval in seconds (default: 60)")
    args = parser.parse_args()

    status_file = args.status_file
    alert_sentinel = status_file + ".ALERT"
    status_dir = os.path.dirname(os.path.abspath(status_file))
    if status_dir:
        os.makedirs(status_dir, exist_ok=True)

    print(f"[experiment-watchdog] log={args.progress_log} stall_minutes={args.stall_minutes} "
          f"pidfile={args.pidfile} status_file={status_file}", flush=True)

    # Grace period: give the campaign stall_minutes from when THIS watchdog
    # started before declaring a stall, since PASS lines carry no completion
    # timestamp of their own -- we track wall-clock time between polls
    # instead of trying to reconstruct historical completion times.
    last_pass_count = None
    last_progress_wallclock = time.time()
    # Failure alerts fire only on INCREASES: fail_count is cumulative over the
    # whole log, so alerting whenever it is nonzero re-alerts every poll on
    # the same historical failure forever. Baseline = count at watchdog start.
    last_fail_count = None

    while True:
        loop_start = time.time()
        alerts = []

        text = read_log(args.progress_log)
        if text is None:
            alerts.append(f"progress log not found: {args.progress_log}")
            report = {"pass_count": 0, "fail_count": 0, "fails": [], "current": 0, "total": 0, "remaining": 0, "batch_complete": False}
        else:
            report = analyze_log(text)

            if last_pass_count is None:
                last_pass_count = report["pass_count"]
            elif report["pass_count"] > last_pass_count:
                last_pass_count = report["pass_count"]
                last_progress_wallclock = time.time()

            stall_seconds = time.time() - last_progress_wallclock
            stalled = (not report["batch_complete"]) and stall_seconds >= args.stall_minutes * 60
            if stalled:
                alerts.append(
                    f"no new completed run in {stall_seconds / 60.0:.1f}m "
                    f"(threshold {args.stall_minutes}m); pass_count={report['pass_count']} "
                    f"progress={report['current']}/{report['total']}"
                )

            if last_fail_count is None:
                last_fail_count = report["fail_count"]
            elif report["fail_count"] > last_fail_count:
                alerts.append(
                    f"{report['fail_count'] - last_fail_count} NEW FAILED run(s) "
                    f"(total {report['fail_count']}): {report['fails'][-3:]}"
                )
                last_fail_count = report["fail_count"]

        pid = None
        pid_status = None
        if args.pidfile:
            try:
                with open(args.pidfile) as f:
                    pid = int(f.read().strip())
                pid_status = "alive" if pid_alive(pid) else "dead"
            except FileNotFoundError:
                pid_status = "pidfile-not-found"
            except (ValueError, OSError) as exc:
                pid_status = f"pidfile-error: {exc}"

            if pid_status == "dead" and report.get("remaining", 0) > 0:
                alerts.append(
                    f"runner process pid={pid} is dead but {report['remaining']} run(s) remain "
                    f"({report['current']}/{report['total']})"
                )

        level = "ALERT" if alerts else "OK"
        record = {
            "ts": now_iso(),
            "watchdog": "experiment",
            "progress_log": args.progress_log,
            "level": level,
            "report": report,
            "pidfile": args.pidfile,
            "pid": pid,
            "pid_status": pid_status,
            "stall_minutes_threshold": args.stall_minutes,
            "alerts": alerts,
        }

        with open(status_file, "a") as f:
            f.write(json.dumps(record) + "\n")

        if level == "ALERT":
            touch(alert_sentinel)
            print(f"[experiment-watchdog] {record['ts']} ALERT ({len(alerts)}): {alerts}", flush=True)
        else:
            print(f"[experiment-watchdog] {record['ts']} OK "
                  f"({report['current']}/{report['total']}, pass={report['pass_count']}, "
                  f"fail={report['fail_count']}, pid={pid_status})", flush=True)

        elapsed = time.time() - loop_start
        time.sleep(max(0.0, args.interval - elapsed))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
