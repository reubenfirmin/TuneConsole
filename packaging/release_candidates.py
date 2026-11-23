"""Validate retained release candidates before a publication-only retry."""
import argparse
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlencode

from promote_release import Backend
from candidate_provenance import check_identity

EXPECTED = ("release-wheel", "release-flatpak-x86_64", "release-flatpak-aarch64",
            "release-macos-arm64", "release-macos-x86_64",
            "release-container-amd64", "release-container-arm64",
            "release-provenance", "release-flatpak-repository")
RECOVERED = ("release-recovered-platforms", "release-provenance", "release-flatpak-repository")
BUILD_WORKFLOW = ".github/workflows/prepare-release.yml"
PR_WORKFLOW = ".github/workflows/ci.yml"
RELEASE_WORKFLOW = ".github/workflows/release.yml"


class CandidateUnavailable(ValueError):
    pass


def pages(backend, path, key=None):
    page = 1
    while True:
        response = backend.api(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        rows = response.get(key) if key and isinstance(response, dict) else response
        if not isinstance(rows, list):
            raise RuntimeError(f"Cannot read {path}; an outage is not a missing candidate")
        yield from rows
        if len(rows) < 100:
            return
        page += 1


def trusted_pr_heads(backend, revision):
    pulls = pages(backend, f"repos/{backend.repository}/commits/{revision}/pulls")
    return {pull["head"]["sha"] for pull in pulls
            if pull["merged_at"] and pull["merge_commit_sha"] == revision
            and pull["user"]["login"] == backend.repository.split("/")[0]
            and (pull["head"].get("repo") or {}).get("full_name") == backend.repository
            and pull["base"]["ref"] == "main" and pull["base"]["repo"]["full_name"] == backend.repository}


def workflow_runs(backend, path, **filters):
    endpoint = f"repos/{backend.repository}/actions/workflows/{Path(path).name}/runs?{urlencode(filters)}"
    return pages(backend, endpoint, "workflow_runs")


def prepared_runs(backend, revision, heads):
    for head in sorted(heads):
        yield from workflow_runs(backend, PR_WORKFLOW, event="pull_request", head_sha=head, status="completed")
    yield from workflow_runs(backend, BUILD_WORKFLOW, branch="main", event="workflow_dispatch",
                             head_sha=revision, status="completed")


def discover(backend, revision, tag, *, status_file):
    heads = trusted_pr_heads(backend, revision)
    for run in prepared_runs(backend, revision, heads):
        try:
            ids = inspect(backend, str(run["id"]), revision, tag, status_file=status_file, pr_heads=heads)
        except CandidateUnavailable as exc:
            print(f"Candidate run {run['id']}: {exc}", flush=True)
        else:
            return str(run["id"]), ids
    raise CandidateUnavailable(
        f"No complete prepared assets match {tag} at {revision}. "
        "Use the completed maintainer PR candidate or run Prepare release assets on main, then retry publication.")


def read_run(backend, run_id):
    if not re.fullmatch(r"[1-9][0-9]*", str(run_id)):
        raise ValueError("Source run must be a numeric Actions run ID")
    return backend.api(f"repos/{backend.repository}/actions/runs/{run_id}")


def was_tag_build(run, revision, tag):
    return bool(run and run.get("path") == RELEASE_WORKFLOW and run.get("event") == "push"
                and run.get("head_branch") == tag and run.get("head_sha") == revision)


def list_artifacts(backend, run_id):
    return list(pages(backend, f"repos/{backend.repository}/actions/runs/{run_id}/artifacts", "artifacts"))


def availability(artifacts, expected):
    rows, ids = [], []
    for name in expected:
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
    return rows, ",".join(ids)


def require_complete(rows, run_id):
    missing = ", ".join(f"{row['name']} ({row['state']})" for row in rows if row["state"] != "complete")
    if missing:
        raise CandidateUnavailable(f"Candidate set incomplete for run {run_id}: {missing}")


def recover(backend, run_id, revision, tag):
    run = read_run(backend, run_id)
    if not was_tag_build(run, revision, tag) or run.get("status") != "completed":
        raise CandidateUnavailable("Recovery requires a completed original tag build at the exact revision")
    rows, ids = availability(list_artifacts(backend, run_id), EXPECTED[:-2])
    require_complete(rows, run_id)
    return ids


def inspect(backend, run_id, revision, tag, *, status_file, pr_heads=None):
    run = read_run(backend, run_id)
    tagged = was_tag_build(run, revision, tag)
    manual = bool(run and run.get("event") == "workflow_dispatch" and run.get("head_branch") == "main")
    path = run.get("path") if run else None
    pr = bool(path == PR_WORKFLOW and run.get("event") == "pull_request"
              and (run.get("head_repository") or {}).get("full_name") == backend.repository)
    if pr:
        heads = trusted_pr_heads(backend, revision) if pr_heads is None else pr_heads
        pr = run.get("head_sha") in heads
    if not (pr or tagged or manual and path in {RELEASE_WORKFLOW, BUILD_WORKFLOW}):
        raise CandidateUnavailable("Source run must build this exact tag and commit through a trusted release or PR workflow")
    if run.get("status") != "completed":
        raise CandidateUnavailable("Wait for the source run to complete before reusing its candidates")
    artifacts = list_artifacts(backend, run_id)
    recovered = path == BUILD_WORKFLOW and any(row["name"] == RECOVERED[0] for row in artifacts)
    rows, ids = availability(artifacts, RECOVERED if recovered else EXPECTED)
    status = {"tag": tag, "revision": revision, "source_run": str(run_id), "assets": rows}
    Path(status_file).write_text(json.dumps(status, indent=2) + "\n")
    summary = (f"Candidate source: https://github.com/{backend.repository}/actions/runs/{run_id}\n\n"
               "| Candidate | State | Bytes |\n| --- | --- | --- |\n" +
               "".join(f"| {row['name']} | {row['state']} | {row['bytes'] or 0} |\n" for row in rows))
    print(summary)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as stream:
            stream.write(summary + "\n")
    require_complete(rows, run_id)
    with tempfile.TemporaryDirectory(prefix="release-provenance-") as directory:
        backend.run("gh", "run", "download", str(run_id), "--repo", backend.repository,
                    "--name", "release-provenance", "--dir", directory)
        try:
            provenance = json.loads((Path(directory) / "release-provenance.json").read_text())
        except (FileNotFoundError, ValueError) as exc:
            raise CandidateUnavailable("The retained candidate provenance could not be read") from exc
        if not isinstance(provenance, dict):
            raise CandidateUnavailable("The retained candidate provenance is invalid")
    build_revision = provenance.get("revision") if pr else revision
    tree = None
    if pr:
        if not isinstance(build_revision, str) or not re.fullmatch(r"[0-9a-f]{40}", build_revision):
            raise CandidateUnavailable("PR candidate did not record its build commit")
        commit_endpoint = f"repos/{backend.repository}/git/commits"
        built = backend.api(f"{commit_endpoint}/{build_revision}")
        tagged_commit = backend.api(f"{commit_endpoint}/{revision}")
        if not built or not tagged_commit or run["head_sha"] not in {parent["sha"] for parent in built["parents"]}:
            raise CandidateUnavailable("PR candidate must come from the merged PR's tested merge commit")
        tree = tagged_commit["tree"]["sha"]
        if built["tree"]["sha"] != tree:
            raise CandidateUnavailable("PR candidate source tree differs from the tagged source tree")
    try:
        check_identity(provenance, tag, build_revision, run_id, backend.repository, tree=tree)
    except ValueError as exc:
        raise CandidateUnavailable(str(exc)) from exc
    status.update(build_revision=build_revision, build_tree=tree or "")
    Path(status_file).write_text(json.dumps(status, indent=2) + "\n")
    return ids


def publication_clock(backend, revision, tag, run_id):
    current = backend.api(f"repos/{backend.repository}/actions/runs/{run_id}")
    started = current["created_at"]
    if current["event"] == "workflow_dispatch":
        original = workflow_runs(backend, RELEASE_WORKFLOW, event="push", branch=tag, head_sha=revision)
        started = min((run["created_at"] for run in original), default=started)
    return {"release_started_at": started}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("tag")
    parser.add_argument("revision")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--status-file", type=Path, default=Path("candidate-status.json"))
    parser.add_argument("--recover", action="store_true", help="Select retained platform artifacts for repair")
    args = parser.parse_args()
    backend = Backend(args.repository)
    source_run = args.run_id
    if args.recover:
        ids = recover(backend, source_run, args.revision, args.tag)
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write(f"artifact_ids={ids}\n")
        raise SystemExit(0)
    if source_run:
        ids = inspect(backend, source_run, args.revision, args.tag, status_file=args.status_file)
    else:
        source_run, ids = discover(backend, args.revision, args.tag, status_file=args.status_file)
    clock = publication_clock(backend, args.revision, args.tag, os.environ["GITHUB_RUN_ID"])
    status = json.loads(args.status_file.read_text())
    status.update(clock, repository=args.repository)
    args.status_file.write_text(json.dumps(status, indent=2) + "\n")
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a") as stream:
            stream.write(f"artifact_ids={ids}\nsource_run={source_run}\nrelease_started_at={clock['release_started_at']}\n"
                         f"build_revision={status['build_revision']}\nbuild_tree={status['build_tree']}\n")
