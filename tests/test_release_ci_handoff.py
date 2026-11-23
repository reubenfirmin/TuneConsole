"""Final-version release provenance and timing without a test-history dependency."""
import importlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "packaging"))
provenance = importlib.import_module("candidate_provenance")
candidates = importlib.import_module("release_candidates")
timing = importlib.import_module("release_timing")
receipt = importlib.import_module("container.receipt")
policy = importlib.import_module("release_policy")


def metadata():
    return {"schema": 1, "tag": "v1.2.3", "version": "1.2.3", "revision": "a" * 40,
            "source_run": "123", "repository": "owner/repo",
            "sha256": dict.fromkeys(provenance.MACOS_NOTARIZATION_RECEIPTS, "b" * 64),
            "architectures": {"macos": ["arm64", "x86_64"], "flatpak": ["aarch64", "x86_64"],
                              "container": ["amd64", "arm64"]}}


@pytest.mark.parametrize("field,value", [("tag", "v1.2.4"), ("version", "0.0.0.dev0"),
                                         ("revision", "b" * 40), ("source_run", "999"),
                                         ("repository", "other/repo")])
def test_different_revision_version_run_or_repository_cannot_reuse_candidates(field, value):
    data = metadata()
    data[field] = value
    with pytest.raises(ValueError, match="exact version"):
        provenance.check_identity(data, "v1.2.3", "a" * 40, "123", "owner/repo")


def test_tampered_payload_cannot_pass_handoff(tmp_path):
    data = metadata()
    names = (provenance.release_payload_names(data["version"]) | provenance.REPOSITORY_ARCHIVES
             | provenance.MACOS_NOTARIZATION_RECEIPTS)
    data["sha256"] = {}
    for name in names:
        payload = tmp_path / name
        payload.write_bytes(b"recorded build bytes")
        data["sha256"][name] = provenance.digest(payload)
    name = "TuneConsole.flatpak"
    (tmp_path / name).write_bytes(b"changed after the release build")
    (tmp_path / "release-provenance.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="recorded bytes"):
        provenance.verify(tmp_path, "v1.2.3", "a" * 40, "123", "owner/repo")
    del data["sha256"][name]
    (tmp_path / "release-provenance.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="missing payload hashes: TuneConsole.flatpak"):
        provenance.verify(tmp_path, "v1.2.3", "a" * 40, "123", "owner/repo")


def test_missing_architecture_cannot_pass_handoff():
    data = metadata()
    data["architectures"]["container"].pop()
    with pytest.raises(ValueError, match="architecture"):
        provenance.check_identity(data, "v1.2.3", "a" * 40, "123", "owner/repo")


def test_candidate_from_before_macos_notarization_cannot_pass_handoff():
    data = metadata()
    del data["sha256"]["macos-notarization-arm64.json"]
    with pytest.raises(ValueError, match="missing macOS notarization receipts"):
        provenance.check_identity(data, "v1.2.3", "a" * 40, "123", "owner/repo")


def test_next_patch_ignores_prereleases_and_uses_numeric_version_order():
    assert policy.next_patch(["v1.2.2", "v1.2.10", "v2.0.0rc1", "old-tag"]) == "v1.2.11"


@pytest.mark.parametrize("changed", [None, "tree", "head", "owner"])
def test_tag_discovers_the_maintainer_pr_candidate_only_for_the_same_merge_tree(tmp_path, changed):
    revision, preview, head, tree = (letter * 40 for letter in "abcd")

    class Release:
        repository = "owner/repo"

        def api(self, path):
            if "/commits/" in path and "/pulls?" in path:
                return [{"merged_at": "time", "merge_commit_sha": revision,
                         "user": {"login": "another" if changed == "owner" else "owner"},
                         "head": {"sha": head, "repo": {"full_name": self.repository}},
                         "base": {"ref": "main", "repo": {"full_name": self.repository}}}]
            if "/workflows/ci.yml/runs?" in path:
                assert f"head_sha={head}" in path
                return {"workflow_runs": [{"id": 123}]}
            if "/workflows/prepare-release.yml/runs?" in path:
                return {"workflow_runs": []}
            if "/artifacts?" in path:
                return {"artifacts": [{"name": name, "id": n + 1, "expired": False, "size_in_bytes": 1}
                                      for n, name in enumerate(candidates.EXPECTED)]}
            if path.endswith(f"/git/commits/{preview}"):
                return {"tree": {"sha": "e" * 40 if changed == "tree" else tree},
                        "parents": [{"sha": "f" * 40 if changed == "head" else head}]}
            if path.endswith(f"/git/commits/{revision}"):
                return {"tree": {"sha": tree}}
            assert path == "repos/owner/repo/actions/runs/123"
            return {"path": candidates.PR_WORKFLOW, "head_sha": head, "event": "pull_request",
                    "head_repository": {"full_name": self.repository}, "status": "completed"}

        def run(self, *args):
            directory = Path(args[args.index("--dir") + 1])
            (directory / "release-provenance.json").write_text(json.dumps(dict(metadata(), revision=preview, tree=tree)))

    status = tmp_path / "status.json"
    if changed:
        with pytest.raises(candidates.CandidateUnavailable, match="No complete prepared assets"):
            candidates.discover(Release(), revision, "v1.2.3", status_file=status)
    else:
        assert candidates.discover(Release(), revision, "v1.2.3", status_file=status) == ("123", "1,2,3,4,5,6,7,8,9")
        assert json.loads(status.read_text())["build_revision"] == preview


@pytest.mark.parametrize("alias_first", [False, True])
def test_registry_receipt_uses_the_pushed_manifest_independent_of_local_alias_order(monkeypatch, alias_first):
    image = "ghcr.io/owner/repo"
    digest = "sha256:" + "a" * 64
    revision = "b" * 40
    remote = f"{image}@{digest}"
    local_aliases = [remote, f"repo@{digest}"]
    if alias_first:
        local_aliases.reverse()
    registry = {f"{image}:candidate": digest, remote: json.dumps({
        "os": "linux", "architecture": "amd64", "config": {"Labels": {
            "org.opencontainers.image.version": "1.2.3", "org.opencontainers.image.revision": revision,
        }},
    })}

    def inspect(reference, _template):
        if reference == "tuneconsole:candidate":
            pytest.fail(f"Read local aliases {local_aliases}")
        return registry[reference]

    monkeypatch.setattr(receipt, "inspect", inspect)
    expected = {"arch": "amd64", "version": "1.2.3", "digest": remote}
    assert receipt.capture(image, "candidate", "amd64", "1.2.3", revision) == expected
    assert receipt.repair(image, dict(expected, digest=f"repo@{digest}"), "amd64", "1.2.3", revision) == expected
    with pytest.raises(ValueError, match="does not match"):
        receipt.capture(image, "candidate", "arm64", "1.2.3", revision)
    registry[f"{image}:candidate"] = "missing"
    with pytest.raises(ValueError, match="manifest digest"):
        receipt.capture(image, "candidate", "amd64", "1.2.3", revision)


@pytest.mark.parametrize("event,branch,head", [("push", "v1.2.3", "a" * 40),
                                             ("workflow_dispatch", "main", "b" * 40)])
def test_release_builds_require_matching_provenance_without_looking_up_ci(tmp_path, event, branch, head):
    class Release:
        repository = "owner/repo"

        def api(self, path):
            if "/artifacts?" in path:
                return {"artifacts": [{"name": name, "id": n + 1, "expired": False, "size_in_bytes": 1}
                                      for n, name in enumerate(candidates.EXPECTED)]}
            assert path == "repos/owner/repo/actions/runs/123"
            return {"path": ".github/workflows/release.yml", "head_sha": head, "event": event,
                    "head_branch": branch, "status": "completed", "conclusion": "failure"}

        def run(self, *args):
            directory = Path(args[args.index("--dir") + 1])
            (directory / "release-provenance.json").write_text(json.dumps(metadata()))

    assert candidates.inspect(Release(), "123", "a" * 40, "v1.2.3", status_file=tmp_path / "status.json") == "1,2,3,4,5,6,7,8,9"
    with pytest.raises(ValueError, match="exact (tag|version)"):
        candidates.inspect(Release(), "123", "b" * 40 if event == "workflow_dispatch" else "a" * 40,
                           "v1.2.3" if event == "workflow_dispatch" else "v1.2.4", status_file=tmp_path / "status.json")


def jobs(completed="2026-10-23T12:11:00Z", conclusion="success"):
    return [{"name": "promote", "created_at": "2026-10-23T12:01:00Z", "started_at": "2026-10-23T12:01:20Z", "steps": [
        {"name": "Verify anonymous downloads and signed public update channels", "status": "completed",
         "conclusion": conclusion, "started_at": "2026-10-23T12:10:00Z", "completed_at": completed},
    ]}]


def test_clock_includes_time_before_runner_and_does_not_restart_at_promotion():
    metrics = timing.measure(jobs(), "2026-10-23T12:00:00Z")
    assert metrics["elapsed_seconds"] == 660
    assert metrics["within_budget"]
    assert next(row for row in metrics["stages"] if row["stage"] == "promote runner queue")["elapsed_seconds"] == 20


def test_late_or_failed_publication_does_not_claim_budget_success():
    late = timing.measure(jobs("2026-10-23T12:16:00Z"), "2026-10-23T12:00:00Z")
    assert late["elapsed_seconds"] == 960 and not late["within_budget"]
    failed = timing.measure(jobs(conclusion="failure"), "2026-10-23T12:00:00Z")
    assert failed["elapsed_seconds"] is None and not failed["within_budget"]


def test_recovery_clock_uses_original_tag_request_without_looking_up_ci():
    class History:
        repository = "owner/repo"

        def api(self, path):
            if path == "repos/owner/repo/actions/runs/123":
                return {"created_at": "2026-10-23T14:30:00Z", "event": "workflow_dispatch"}
            assert "/workflows/release.yml/runs?event=push&branch=v1.2.3&head_sha=" in path
            return {"workflow_runs": [{"created_at": "2026-10-23T12:01:00Z"}]}

    assert candidates.publication_clock(History(), "a" * 40, "v1.2.3", "123") == {"release_started_at": "2026-10-23T12:01:00Z"}
