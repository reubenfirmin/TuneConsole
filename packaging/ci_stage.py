"""Bound a CI setup/build stage and retain elapsed time and recovery context."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def record(path, entry):
    previous = json.loads(path.read_text()) if path.exists() else []
    if entry["status"] != "running":
        previous = [row for row in previous if not (row["stage"] == entry["stage"] and row["attempt"] == entry["attempt"])]
    previous.append(entry)
    path.write_text(json.dumps(previous, indent=2) + "\n")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as stream:
            stream.write(f"Stage **{entry['stage']}**, attempt {entry['attempt']}: {entry['status']}"
                         f" ({entry.get('elapsed_seconds', 0):.1f}s).\n\n")


def execute(label, command, *, timeout, attempts=1, timings=Path("ci-stage-timings.json")):
    if not command or timeout <= 0 or not 1 <= attempts <= 3:
        raise ValueError("A command, positive timeout and one to three attempts are required")
    result = 1
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        entry = {"stage": label, "attempt": attempt, "status": "running", "budget_seconds": timeout}
        record(timings, entry)
        try:
            process = subprocess.Popen(command, start_new_session=True)
        except OSError as exc:
            entry.update(status="failed to start", elapsed_seconds=time.monotonic() - started,
                         error=type(exc).__name__)
            record(timings, entry)
            return 127
        try:
            result = process.wait(timeout=timeout)
            entry["status"] = "success" if result == 0 else "failed"
        except subprocess.TimeoutExpired:
            # Reap the process group so a timed-out downloader cannot silently
            # survive while the next attempt or an application build begins.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            # The leader can exit on SIGTERM while descendants ignore it. Always
            # signal the remaining group before another attempt is allowed.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            entry["status"], result = "timed out", 124
        entry["elapsed_seconds"] = time.monotonic() - started
        record(timings, entry)
        if result == 0:
            return 0
    print(f"Stage {label} {entry['status']}. Retry this failed job; keep successful candidate artifacts.", flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label")
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--timings", type=Path, default=Path("ci-stage-timings.json"))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    raise SystemExit(execute(args.label, command, timeout=args.timeout, attempts=args.attempts, timings=args.timings))
