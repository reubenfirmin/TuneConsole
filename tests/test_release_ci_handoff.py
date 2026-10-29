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


def metadata():
    return {"schema": 1, "tag": "v1.2.3", "version": "1.2.3", "revision": "a" * 40,
            "source_run": "123", "repository": "owner/repo", "sha256": {"payload": "b" * 64},
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
    (tmp_path / "release-provenance.json").write_text(json.dumps(metadata()))
    (tmp_path / "payload").write_bytes(b"changed after the release build")
    with pytest.raises(ValueError, match="recorded bytes"):
        provenance.verify(tmp_path, "v1.2.3", "a" * 40, "123", "owner/repo")


def test_missing_architecture_cannot_pass_handoff():
    data = metadata()
    data["architectures"]["container"].pop()
    with pytest.raises(ValueError, match="architecture"):
        provenance.check_identity(data, "v1.2.3", "a" * 40, "123", "owner/repo")


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


def test_recovery_clock_uses_original_release_start_without_looking_up_ci():
    class History:
        repository = "owner/repo"

        def api(self, path):
            assert path == "repos/owner/repo/actions/runs/123"
            return {"created_at": "2026-10-23T12:01:00Z", "updated_at": "2026-10-23T14:30:00Z"}

    assert candidates.source_clock(History(), "123") == {"release_started_at": "2026-10-23T12:01:00Z"}
