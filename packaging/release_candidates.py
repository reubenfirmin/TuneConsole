"""Validate retained release candidates before a publication-only retry."""
import argparse
import json
import os
from pathlib import Path
import re
import tempfile

from promote_release import Backend
from candidate_provenance import check_identity

EXPECTED = ("release-wheel", "release-flatpak-x86_64", "release-flatpak-aarch64",
            "release-macos-arm64", "release-macos-x86_64",
            "release-container-amd64", "release-container-arm64",
            "release-provenance", "release-flatpak-repository")


def inspect(backend, run_id, revision, tag, *, status_file, allow_active=False):
    if not re.fullmatch(r"[1-9][0-9]*", str(run_id)):
        raise ValueError("Source run must be a numeric Actions run ID")
    run = backend.api(f"repos/{backend.repository}/actions/runs/{run_id}")
    tagged = bool(run and run.get("event") == "push" and run.get("head_branch") == tag
                  and run.get("head_sha") == revision)
    manual = bool(run and run.get("event") == "workflow_dispatch" and run.get("head_branch") == "main")
    if not run or run.get("path") != ".github/workflows/release.yml" or not (tagged or manual):
        raise ValueError("Source run must build this exact tag and commit through release.yml on main or a release tag")
    if run.get("status") != "completed" and not (allow_active and run.get("status") == "in_progress"):
        raise ValueError("Wait for the source run to complete before reusing its candidates")
    artifacts = []
    page = 1
    while True:
        response = backend.api(f"repos/{backend.repository}/actions/runs/{run_id}/artifacts?per_page=100&page={page}")
        if not isinstance(response, dict) or not isinstance(response.get("artifacts"), list):
            raise RuntimeError("Cannot read source artifacts; an outage is not a missing candidate")
        artifacts.extend(response["artifacts"])
        if len(response["artifacts"]) < 100:
            break
        page += 1
    rows, ids = [], []
    for name in EXPECTED:
        matches = [a for a in artifacts if a["name"] == name]
        state = "missing"
        if len(matches) > 1:
            state = "ambiguous"
        elif matches:
            asset = matches[0]
            state = "expired" if asset["expired"] else "complete" if asset["size_in_bytes"] > 0 else "empty"
            if state == "complete":
                ids.append(str(asset["id"]))
        rows.append({"name": name, "state": state,
                     "bytes": matches[0]["size_in_bytes"] if len(matches) == 1 else None})
    status = {"tag": tag, "revision": revision, "source_run": str(run_id), "assets": rows}
    Path(status_file).write_text(json.dumps(status, indent=2) + "\n")
    summary = (f"Candidate source: https://github.com/{backend.repository}/actions/runs/{run_id}\n\n"
               "| Candidate | State | Bytes |\n| --- | --- | --- |\n" +
               "".join(f"| {row['name']} | {row['state']} | {row['bytes'] or 0} |\n" for row in rows))
    print(summary)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as stream:
            stream.write(summary + "\n")
    if any(row["state"] != "complete" for row in rows):
        raise ValueError("Candidate set incomplete; rerun only failed source jobs. Expired artifacts require rebuilding their platform.")
    with tempfile.TemporaryDirectory(prefix="release-provenance-") as directory:
        backend.run("gh", "run", "download", str(run_id), "--repo", backend.repository,
                    "--name", "release-provenance", "--dir", directory)
        provenance = json.loads((Path(directory) / "release-provenance.json").read_text())
    check_identity(provenance, tag, revision, run_id, backend.repository)
    return ",".join(ids)


def source_clock(backend, run_id):
    """Include original build time in publication retries without consulting test history."""
    run = backend.api(f"repos/{backend.repository}/actions/runs/{run_id}")
    return {"release_started_at": run["created_at"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("tag")
    parser.add_argument("revision")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--status-file", type=Path, default=Path("candidate-status.json"))
    args = parser.parse_args()
    backend = Backend(args.repository)
    source_run = args.run_id
    ids = inspect(backend, source_run, args.revision, args.tag, status_file=args.status_file,
                  allow_active=source_run == os.environ.get("GITHUB_RUN_ID"))
    clock = source_clock(backend, source_run)
    status = json.loads(args.status_file.read_text())
    status.update(clock, repository=args.repository)
    args.status_file.write_text(json.dumps(status, indent=2) + "\n")
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a") as stream:
            stream.write(f"artifact_ids={ids}\nsource_run={source_run}\nrelease_started_at={clock['release_started_at']}\n")
