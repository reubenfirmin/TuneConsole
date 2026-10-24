"""Measure hosted source checks and compare repeated serial/sharded control runs."""
import argparse
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import re
import statistics

from promote_release import Backend

SCOPE = "Planning through the stable checks gate, including queues, setup and coverage. The reporting job and downstream packaging are excluded."


def seconds(start, end):
    if not start or not end:
        return None
    elapsed = (datetime.fromisoformat(end.replace("Z", "+00:00")) -
               datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()
    return elapsed if elapsed >= 0 else None


def measure(jobs, revision, shards=2, suites=None):
    rows = []
    orchestration = []
    for job in jobs:
        # GitHub prefixes jobs from a reusable workflow with the caller's job name.
        role = job["name"].rsplit(" / ", 1)[-1]
        row = {"job": job["name"], "role": role, "url": job["html_url"], "conclusion": job["conclusion"],
               "queue_seconds": seconds(job.get("created_at"), job.get("started_at")),
               "runner_seconds": seconds(job.get("started_at"), job.get("completed_at")),
               "created_at": job.get("created_at"), "completed_at": job.get("completed_at")}
        if role in {"plan", "coverage", "checks"}:
            orchestration.append(row)
            continue
        if not re.fullmatch(r"(?:unit|browser) \([1-9][0-9]*/[1-9][0-9]*\)", role):
            continue
        steps = job.get("steps", [])
        test = next((step for step in steps if step["name"].startswith("Run the ")), None)
        row.update(setup_seconds=seconds(job.get("started_at"), test.get("started_at")) if test else None,
                   test_seconds=seconds(test.get("started_at"), test.get("completed_at")) if test else None)
        rows.append(row)
    required = rows + orchestration
    expected = [f"{suite} ({index}/{shards})" for suite in ("unit", "browser") for index in range(1, shards + 1)]
    expected.extend(("plan", "coverage", "checks"))
    complete = (shards in (1, 2, 4) and Counter(row["role"] for row in required) == Counter(expected)
                and all(row["conclusion"] == "success" and row["runner_seconds"] is not None
                        and row["queue_seconds"] is not None for row in required)
                and all(row["test_seconds"] is not None and row["setup_seconds"] is not None for row in rows))
    return {"revision": revision, "shards": shards, "complete": complete, "jobs": rows, "suites": suites or {}, "scope": SCOPE,
            "orchestration_jobs": orchestration,
            "elapsed_seconds": seconds(min(row["created_at"] for row in required), max(row["completed_at"] for row in required))
                if required and all(row["created_at"] and row["completed_at"] for row in required) else None,
            "matrix_elapsed_seconds": seconds(min(row["created_at"] for row in rows), max(row["completed_at"] for row in rows))
                if rows and all(row["created_at"] and row["completed_at"] for row in rows) else None,
            "runner_minutes": sum(row["runner_seconds"] or 0 for row in required) / 60,
            "test_runner_minutes": sum(row["runner_seconds"] or 0 for row in rows) / 60,
            "failed_or_cancelled_jobs": [row["url"] for row in required if row["conclusion"] != "success"],
            "comparison": "Compare repeated clean runs at the same revision, including cold setup. No speedup inferred from a single run."}


def compare(measurements):
    """Refuse single-run claims, different revisions, omitted cases and duplicate inputs."""
    counts = sorted({row["shards"] for row in measurements})
    groups = {total: [row for row in measurements if row["shards"] == total] for total in counts}
    if (len(groups) != 2 or any(len(group) < 2 for group in groups.values())
            or not all(row["complete"] and row["shards"] in (1, 2, 4) and row["elapsed_seconds"] > 0 for row in measurements)):
        raise ValueError("Compare at least two passing baseline runs and two passing sharded runs")
    revisions = {row["revision"] for row in measurements}
    identities = {(row["run_id"], row["run_attempt"]) for row in measurements}
    if len(revisions) != 1 or len(identities) != len(measurements):
        raise ValueError("Comparison needs distinct runs of the same source revision")
    coverage = []
    for row in measurements:
        if set(row["suites"]) != {"unit", "browser"}:
            raise ValueError("Both verified suite timing reports are required")
        for suite in ("unit", "browser"):
            report = row["suites"][suite]
            if (report["suite"] != suite or report["revision"] != row["revision"] or report["shards"] != row["shards"]
                    or report["cases"] < 1 or not report["case_digest"]):
                raise ValueError("Suite timing reports do not match the measured source checks")
        coverage.append(tuple((suite, row["suites"][suite]["cases"], row["suites"][suite]["case_digest"])
                              for suite in ("unit", "browser")))
    if len(set(coverage)) != 1:
        raise ValueError("Baseline and shards must execute the same parametrized cases")
    summaries = {}
    for total, rows in groups.items():
        elapsed = [row["elapsed_seconds"] for row in rows]
        summaries[str(total)] = {
            "runs": [row["url"] for row in rows], "samples": len(rows),
            "median_elapsed_seconds": statistics.median(elapsed), "elapsed_range_seconds": [min(elapsed), max(elapsed)],
            "median_runner_minutes": statistics.median(row["runner_minutes"] for row in rows),
            "median_setup_seconds": statistics.median(sum(job["setup_seconds"] for job in row["jobs"]) for row in rows),
            "median_phase_seconds": {phase: statistics.median(sum(row["suites"][suite]["phase_seconds"][phase]
                                                                   for suite in ("unit", "browser")) for row in rows)
                                     for phase in ("setup", "call", "teardown")},
            "skipped_cases": [{"run": row["url"], **{suite: row["suites"][suite]["skipped"]
                                                       for suite in ("unit", "browser")}} for row in rows],
        }
    baseline, sharded = (summaries[str(total)] for total in counts)
    return {"revision": next(iter(revisions)), "measurements": summaries,
            "baseline_shards": counts[0], "candidate_shards": counts[1],
            "wall_time_reduction_percent": 100 * (1 - sharded["median_elapsed_seconds"] / baseline["median_elapsed_seconds"]),
            "runner_minute_change": sharded["median_runner_minutes"] - baseline["median_runner_minutes"],
            "scope": SCOPE}


def collect(backend, run_id, attempt, shards, suites):
    run = backend.api(f"repos/{backend.repository}/actions/runs/{run_id}")
    attempt = attempt or run["run_attempt"]
    jobs, page = [], 1
    while True:
        response = backend.api(f"repos/{backend.repository}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}")
        jobs.extend(response["jobs"])
        if len(response["jobs"]) < 100:
            break
        page += 1
    revisions = {report["revision"] for report in suites.values()}
    # PR jobs check out GITHUB_SHA (the synthetic merge), while the run API's
    # head_sha identifies the PR branch. Retain both instead of relabelling tests.
    tested_revision = next(iter(revisions)) if len(revisions) == 1 else run["head_sha"]
    result = measure(jobs, tested_revision, shards, suites)
    result["complete"] = (result["complete"] and set(suites) == {"unit", "browser"} and len(revisions) == 1
                          and all(report["suite"] == suite and report["shards"] == shards
                                  for suite, report in suites.items())
                          and (run["event"] == "pull_request" or tested_revision == run["head_sha"]))
    result.update(run_id=str(run_id), run_attempt=str(attempt), url=run["html_url"], head_revision=run["head_sha"])
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare", type=Path, nargs="+", metavar="METRICS_JSON")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID"))
    parser.add_argument("--attempt", default=os.environ.get("GITHUB_RUN_ATTEMPT"))
    parser.add_argument("--shards", type=int, choices=(1, 2, 4), default=int(os.environ.get("SHARD_TOTAL", "4")))
    parser.add_argument("--timings", type=Path, default=Path("evidence"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.compare:
        result = compare([json.loads(path.read_text()) for path in args.compare])
        output = args.output or Path("ci-test-comparison.json")
    else:
        if not args.repository or not args.run_id:
            parser.error("--repository and --run-id are required outside GitHub Actions")
        suites = {suite: json.loads(path.read_text()) for suite in ("unit", "browser")
                  if (path := args.timings / f"{suite}-timings.json").is_file()}
        result = collect(Backend(args.repository), args.run_id, args.attempt, args.shards, suites)
        output = args.output or Path("ci-test-metrics.json")
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if not args.compare and os.environ.get("GITHUB_STEP_SUMMARY"):
        status = "complete" if result["complete"] else "incomplete; diagnostics only"
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(f"Source test measurement ({status}): {result['elapsed_seconds']}s including planning, runner queues, coverage and checks; "
                         f"{result['runner_minutes']:.2f} total runner minutes "
                         f"({result['test_runner_minutes']:.2f} in test runners).\n\n"
                         "Per-shard JUnit, phase timings, skipped cases and browser failure traces are retained. "
                         "Compare repeated runs on the same passing revision before changing concurrency.\n")
