"""Plan versioned candidates and reuse only successful source checks for this exact SHA."""
import json
import os

from promote_release import Backend, classify


def successful_runs(backend, revision):
    page = 1
    while True:
        result = backend.api(f"repos/{backend.repository}/actions/workflows/ci.yml/runs?head_sha={revision}&status=success&per_page=100&page={page}")
        if not isinstance(result, dict) or not isinstance(result.get("workflow_runs"), list):
            raise RuntimeError("Cannot verify prior source checks")
        for run in result["workflow_runs"]:
            if (run["head_sha"] == revision and run["status"] == "completed" and run["conclusion"] == "success"
                    and run["event"] in {"push", "workflow_dispatch"}):
                yield run
        if len(result["workflow_runs"]) < 100:
            return
        page += 1


def plan(backend, tag, revision, event, ref):
    result = {"version": "0.0.0.dev0", "tag": "", "reuse_tests": "false", "checked_run": ""}
    if tag:
        if event != "workflow_dispatch" or ref != "refs/heads/main":
            raise ValueError("Release candidates require a trusted manual CI run on main")
        result.update(version=classify(tag).version, tag=tag)
        previous = next(successful_runs(backend, revision), None)
        if previous:
            result.update(reuse_tests="true", checked_run=str(previous["id"]))
    return result


def timing_source(backend):
    """Pin one trusted timing artifact for every shard, even if CI finishes mid-run."""
    result = backend.api(f"repos/{backend.repository}/actions/workflows/ci.yml/runs?branch=main&status=success&per_page=20")
    if not isinstance(result, dict) or not isinstance(result.get("workflow_runs"), list):
        raise RuntimeError("Cannot read the shard timing baseline")
    for run in result["workflow_runs"]:
        if (run["head_branch"] != "main" or run["status"] != "completed" or run["conclusion"] != "success"
                or run["event"] not in {"push", "workflow_dispatch"}):
            continue
        artifacts = backend.api(f"repos/{backend.repository}/actions/runs/{run['id']}/artifacts?per_page=100")["artifacts"]
        matches = [artifact for artifact in artifacts
                   if artifact["name"] == f"source-test-timings-{run['run_attempt']}"
                   and not artifact["expired"] and artifact["size_in_bytes"] > 0]
        if len(matches) == 1:
            return {"timing_run": str(run["id"]), "timing_artifact": str(matches[0]["id"])}
    return {"timing_run": "", "timing_artifact": ""}


if __name__ == "__main__":
    backend = Backend(os.environ["GITHUB_REPOSITORY"])
    result = plan(backend, os.environ.get("RELEASE_TAG", ""),
                  os.environ["GITHUB_SHA"], os.environ["GITHUB_EVENT_NAME"], os.environ["GITHUB_REF"])
    result.update(timing_source(backend))
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        stream.write("".join(f"{name}={value}\n" for name, value in result.items()))
    print(json.dumps(result))
