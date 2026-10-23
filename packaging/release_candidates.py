"""Validate retained release candidates before a publication-only retry."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile

from promote_release import Backend
from candidate_provenance import check_identity
from ci_plan import successful_runs

EXPECTED = ("release-wheel", "release-flatpak-x86_64", "release-flatpak-aarch64",
            "release-macos-arm64", "release-macos-x86_64",
            "release-container-amd64", "release-container-arm64")


def inspect(backend, run_id, revision, tag, *, status_file, allow_active=False):
    if not re.fullmatch(r"[1-9][0-9]*", str(run_id)):
        raise ValueError("Source run must be a numeric Actions run ID")
    run = backend.api(f"repos/{backend.repository}/actions/runs/{run_id}")
    ci = bool(run and run.get("path") == ".github/workflows/ci.yml")
    origin = (".github/workflows/ci.yml", "workflow_dispatch", "main") if ci else (
        ".github/workflows/release.yml", "push", tag)
    if not run or (run.get("head_sha"), run.get("path"), run.get("event"), run.get("head_branch")) != (revision, *origin):
        raise ValueError("Source run must build this exact tag and commit through trusted CI or the original release.yml")
    if ci and run.get("conclusion") != "success":
        raise ValueError("Final release candidates require successful CI")
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
    for name in (*EXPECTED, "release-provenance", "release-flatpak-repository") if ci else EXPECTED:
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
    if ci:
        with tempfile.TemporaryDirectory(prefix="release-provenance-") as directory:
            backend.run("gh", "run", "download", str(run_id), "--repo", backend.repository,
                        "--name", "release-provenance", "--dir", directory)
            provenance = json.loads((Path(directory) / "release-provenance.json").read_text())
        check_identity(provenance, tag, revision, run_id, backend.repository)
    return ",".join(ids)


def find(backend, revision, tag, *, status_file):
    for run in successful_runs(backend, revision):
        try:
            ids = inspect(backend, run["id"], revision, tag, status_file=status_file)
        except ValueError:
            continue
        return str(run["id"]), ids
    raise ValueError(
        f"No complete CI candidates for {tag} at {revision}. Prepare them with "
        f"gh workflow run ci.yml --ref main -f release_tag={tag}; tag that exact tested commit after CI succeeds."
    )


def source_clock(backend, run_id, revision):
    """Keep publication retries anchored to green CI, including legacy builds."""
    run = backend.api(f"repos/{backend.repository}/actions/runs/{run_id}")
    if run["path"] == ".github/workflows/ci.yml":
        checked = run
    else:
        # The legacy source run contains platform builds and a failed promotion.
        # Its updated_at would hide all that time and restart the release clock.
        created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        prior = [row for row in successful_runs(backend, revision)
                 if datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")) <= created]
        if not prior:
            raise ValueError("Cannot establish green CI before the original release; retain candidates and inspect source history")
        checked = max(prior, key=lambda row: row["updated_at"])
    if (checked["head_sha"] != revision or checked["status"] != "completed"
            or checked["conclusion"] != "success" or checked["event"] not in {"push", "workflow_dispatch"}):
        raise ValueError("Release timing requires completed successful CI for this exact revision")
    return {"ci_completed_at": checked["updated_at"], "ci_run": str(checked["id"]),
            "ci_run_attempt": str(checked["run_attempt"])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("tag")
    parser.add_argument("revision")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--status-file", type=Path, default=Path("candidate-status.json"))
    args = parser.parse_args()
    backend = Backend(args.repository)
    if args.run_id == "auto":
        source_run, ids = find(backend, args.revision, args.tag, status_file=args.status_file)
    else:
        source_run = args.run_id
        ids = inspect(backend, source_run, args.revision, args.tag, status_file=args.status_file,
                      allow_active=source_run == os.environ.get("GITHUB_RUN_ID"))
    clock = source_clock(backend, source_run, args.revision)
    status = json.loads(args.status_file.read_text())
    status.update(clock, repository=args.repository)
    args.status_file.write_text(json.dumps(status, indent=2) + "\n")
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a") as stream:
            stream.write(f"artifact_ids={ids}\nsource_run={source_run}\nci_completed_at={clock['ci_completed_at']}\n")
