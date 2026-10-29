"""Measure release builds through verified publication, including runner queues."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from promote_release import Backend
from release_candidates import EXPECTED

BUDGET_SECONDS = 15 * 60
TARGET_SECONDS = 10 * 60
VERIFY_STEP = "Verify anonymous downloads and signed public update channels"


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def duration(start, end):
    if not start or not end:
        return None
    elapsed = (instant(end) - instant(start)).total_seconds()
    return elapsed if elapsed >= 0 else None


def measure(jobs, release_started_at, *, now=None):
    started = instant(release_started_at)
    now = now or datetime.now(timezone.utc)
    stages = []
    verification = []
    blocking = None
    release_jobs = [job for job in jobs if job["name"].rsplit(" / ", 1)[-1] != "summary"]
    dispatches = [instant(job["created_at"]) for job in release_jobs if job.get("created_at")]
    if dispatches:
        stages.append({"stage": "Release requested to first job", "elapsed_seconds": max(0, (min(dispatches) - started).total_seconds()),
                       "result": "complete"})
    for job in release_jobs:
        queued = duration(job.get("created_at"), job.get("started_at"))
        waiting = not job.get("started_at") and job.get("status") in {"queued", "waiting", "pending", "in_progress"}
        if waiting:
            queued = duration(job.get("created_at"), now.isoformat())
        stages.append({"stage": job["name"] + " runner queue", "elapsed_seconds": queued,
                       "result": "waiting" if waiting else job.get("conclusion") if queued is None else "complete"})
        if waiting and blocking is None:
            blocking = stages[-1]["stage"]
        for step in job.get("steps", []):
            elapsed = duration(step.get("started_at"), step.get("completed_at") or
                               (now.isoformat() if step.get("status") == "in_progress" else None))
            stages.append({"stage": job["name"] + " / " + step["name"], "elapsed_seconds": elapsed,
                           "result": step.get("conclusion") or step.get("status", "unknown")})
            if step.get("conclusion") in {"failure", "cancelled", "timed_out"} and blocking is None:
                blocking = stages[-1]["stage"]
            if step.get("status") == "in_progress" and blocking is None:
                blocking = stages[-1]["stage"]
            if (job["name"].rsplit(" / ", 1)[-1] == "promote" and step["name"] == VERIFY_STEP
                    and step.get("conclusion") == "success" and elapsed is not None):
                verification.append(instant(step["completed_at"]))
        if job.get("conclusion") in {"failure", "cancelled", "timed_out"} and blocking is None:
            blocking = job["name"]
    verified = verification[0] if len(verification) == 1 else None
    if len(verification) > 1:
        blocking = "Ambiguous public verification; select one release attempt"
    elapsed = (verified - started).total_seconds() if verified else None
    return {"release_started_at": release_started_at, "verified_at": verified.isoformat() if verified else None,
            "elapsed_seconds": elapsed, "budget_seconds": BUDGET_SECONDS, "target_seconds": TARGET_SECONDS,
            "observed_elapsed_seconds": (now - started).total_seconds(),
            "within_budget": elapsed is not None and 0 <= elapsed <= BUDGET_SECONDS,
            "blocking_stage": blocking if verified is None else None, "stages": stages}


def render(metrics):
    elapsed = metrics["elapsed_seconds"]
    status = f"Verified publication in {elapsed:.1f}s" if elapsed is not None else "Public verification incomplete"
    lines = [status + "; target 600s, limit 900s (clock starts when the release is requested).", "",
             f"Current blocking stage: {metrics['blocking_stage'] or 'none; inspect failed stages below if incomplete'}.", ""]
    if candidate := metrics.get("candidate"):
        source = f"https://github.com/{metrics['repository']}/actions/runs/{candidate['source_run']}"
        lines.extend([f"[{candidate['tag']}]({metrics['url']}) at `{candidate['revision']}`; "
                      f"[release build]({source}); {metrics['artifact_bytes']:,} retained candidate bytes.", ""])
    lines.extend(["| Stage (includes runner queues) | Seconds | Result |", "| --- | --- | --- |"])
    for row in metrics["stages"]:
        seconds = f"{row['elapsed_seconds']:.1f}" if row["elapsed_seconds"] is not None else "pending"
        lines.append(f"| {row['stage']} | {seconds} | {row['result']} |")
    if not metrics["within_budget"]:
        lines.extend(["", "Publication is incomplete or missed its budget. Preserve immutable assets and any channel checkpoint; "
                      "use the publication-only recovery command after inspecting the failed stage."])
    return "\n".join(lines) + "\n"


def collect(backend, run_id, attempt, candidate):
    """Pin the attempt so a recovery cannot hide failed work or reuse an old check."""
    jobs, page = [], 1
    while True:
        data = backend.api(f"repos/{backend.repository}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}")
        jobs.extend(data["jobs"])
        if len(data["jobs"]) < 100:
            break
        page += 1
    metrics = measure(jobs, candidate["release_started_at"])
    metrics.update(repository=backend.repository, run_id=str(run_id), run_attempt=str(attempt),
                   url=f"https://github.com/{backend.repository}/actions/runs/{run_id}/attempts/{attempt}",
                   candidate=candidate,
                   artifact_bytes=sum(row["bytes"] or 0 for row in candidate["assets"]))
    return metrics


def compare(measurements, *, cold_run):
    """Check recorded hosted evidence; local fixtures cannot meet the release target."""
    if len(measurements) < 3 or len({row["run_id"] for row in measurements}) != len(measurements):
        raise ValueError("Three distinct hosted release runs are required")
    cold = next((row for row in measurements if row["run_id"] == str(cold_run)), None)
    if not cold or not any(row["stage"] == "promote / Install publication tools within a separate setup budget"
                           and row["result"] == "success" and row["elapsed_seconds"] is not None
                           for row in cold["stages"]):
        raise ValueError("Identify the cold-setup run after checking its runner logs and successful tool-install stage")
    rows = []
    for metrics in measurements:
        candidate = metrics["candidate"]
        elapsed = duration(metrics["release_started_at"], metrics["verified_at"])
        if (elapsed is None or elapsed > BUDGET_SECONDS or metrics["elapsed_seconds"] != elapsed
                or metrics["release_started_at"] != candidate["release_started_at"]
                or metrics["repository"] != candidate["repository"]
                or not candidate.get("source_run")
                or not candidate.get("revision") or not candidate.get("tag")
                or {row["name"] for row in candidate["assets"]} != set(EXPECTED)
                or any(row["state"] != "complete" or row["bytes"] <= 0 for row in candidate["assets"])
                or metrics["artifact_bytes"] != sum(row["bytes"] for row in candidate["assets"])):
            raise ValueError("Each release needs complete candidates and verified publication within 900 seconds of its request")
        rows.append({"release": metrics["url"], "tag": candidate["tag"], "revision": candidate["revision"],
                     "source": f"https://github.com/{metrics['repository']}/actions/runs/{candidate['source_run']}",
                     "elapsed_seconds": elapsed, "artifact_bytes": metrics["artifact_bytes"]})
    return {"budget_seconds": BUDGET_SECONDS, "runs": rows, "cold_setup_run": cold["url"],
            "cold_setup_review": "Cold setup identified by the reviewer from runner logs; successful tool installation recorded."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare", type=Path, nargs="+", metavar="TIMING_JSON")
    parser.add_argument("--cold-run", help="Hosted run ID whose cold setup has been checked in runner logs")
    parser.add_argument("--candidate-status", type=Path, default=Path("evidence/candidates/candidate-status.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.compare:
        if not args.cold_run:
            parser.error("--compare requires --cold-run after checking the cold setup in runner logs")
        metrics = compare([json.loads(path.read_text()) for path in args.compare], cold_run=args.cold_run)
        output = args.output or Path("release-rehearsals.json")
        result = json.dumps(metrics, indent=2) + "\n"
    else:
        metrics = collect(Backend(os.environ["GITHUB_REPOSITORY"]), os.environ["GITHUB_RUN_ID"],
                          os.environ["GITHUB_RUN_ATTEMPT"], json.loads(args.candidate_status.read_text()))
        output = args.output or Path("release-timing.json")
        result = render(metrics)
        if path := os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(path, "a") as stream:
                stream.write(result)
    output.write_text(json.dumps(metrics, indent=2) + "\n")
    print(result)
    raise SystemExit(0 if args.compare or metrics["within_budget"] else 1)
