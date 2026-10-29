"""Select a measured timing baseline for PR test shards."""
import json
import os
from datetime import datetime
from pathlib import Path

from promote_release import Backend


def timing_source(backend, baseline=None):
    """Pin one trusted timing artifact for every shard, even if CI finishes mid-run."""
    if baseline is None:
        baseline = json.loads((Path(__file__).resolve().parents[1] / '.github/test-durations.json').read_text())
    refreshed = baseline.get('refreshed_at')
    refreshed_at = datetime.fromisoformat(refreshed) if refreshed else None
    result = backend.api(f"repos/{backend.repository}/actions/workflows/ci.yml/runs?event=pull_request&status=success&per_page=20")
    if not isinstance(result, dict) or not isinstance(result.get("workflow_runs"), list):
        raise RuntimeError("Cannot read the shard timing baseline")
    for run in result["workflow_runs"]:
        if (run.get("status") != "completed" or run.get("conclusion") != "success"
                or run.get("event") != "pull_request"
                or (run.get("head_repository") or {}).get("full_name") != backend.repository):
            continue
        started = run.get('run_started_at')
        if refreshed_at and (not started or datetime.fromisoformat(started) <= refreshed_at):
            continue
        artifacts = backend.api(f"repos/{backend.repository}/actions/runs/{run['id']}/artifacts?per_page=100")["artifacts"]
        matches = [artifact for artifact in artifacts
                   if artifact["name"] == f"source-test-timings-{run['run_attempt']}"
                   and not artifact["expired"] and artifact["size_in_bytes"] > 0]
        if len(matches) == 1:
            return {"timing_run": str(run["id"]), "timing_artifact": str(matches[0]["id"])}
    return {"timing_run": "", "timing_artifact": ""}


if __name__ == "__main__":
    result = timing_source(Backend(os.environ["GITHUB_REPOSITORY"]))
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        stream.write("".join(f"{name}={value}\n" for name, value in result.items()))
    print(json.dumps(result))
