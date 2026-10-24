"""Exact-revision test reuse, final-version provenance and honest release timings."""
import importlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "packaging"))
ci_plan = importlib.import_module("ci_plan")
provenance = importlib.import_module("candidate_provenance")
candidates = importlib.import_module("release_candidates")
timing = importlib.import_module("release_timing")


class Source:
    repository = "owner/repo"

    def __init__(self, sha="a" * 40, event="push", conclusion="success"):
        self.prior = {"id": 123, "head_sha": sha, "event": event, "status": "completed", "conclusion": conclusion}

    def api(self, path):
        return {"workflow_runs": [self.prior]}


@pytest.mark.parametrize("sha,event,conclusion,reuse", [
    ("a" * 40, "push", "success", "true"),
    ("a" * 40, "workflow_dispatch", "success", "true"),
    ("b" * 40, "push", "success", "false"),
    ("a" * 40, "pull_request", "success", "false"),
    ("a" * 40, "push", "failure", "false"),
])
def test_preparation_reuses_only_success_for_this_exact_source(sha, event, conclusion, reuse):
    result = ci_plan.plan(Source(sha, event, conclusion), "v1.2.3", "a" * 40, "workflow_dispatch", "refs/heads/main")
    assert result["version"] == "1.2.3"
    assert result["reuse_tests"] == reuse


def test_final_version_preparation_cannot_receive_secrets_on_a_pr():
    with pytest.raises(ValueError, match="trusted manual CI"):
        ci_plan.plan(Source(), "v1.2.3", "a" * 40, "pull_request", "refs/pull/1/merge")


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
    (tmp_path / "payload").write_bytes(b"changed after smoke test")
    with pytest.raises(ValueError, match="smoke-tested bytes"):
        provenance.verify(tmp_path, "v1.2.3", "a" * 40, "123", "owner/repo")


def test_missing_architecture_cannot_pass_handoff():
    data = metadata()
    data["architectures"]["container"].pop()
    with pytest.raises(ValueError, match="architecture"):
        provenance.check_identity(data, "v1.2.3", "a" * 40, "123", "owner/repo")


def test_ci_candidate_lookup_requires_provenance_not_just_success(tmp_path):
    class CI:
        repository = "owner/repo"

        def api(self, path):
            if "/artifacts?" in path:
                return {"artifacts": [{"name": name, "id": n + 1, "expired": False, "size_in_bytes": 1}
                                      for n, name in enumerate((*candidates.EXPECTED, "release-provenance", "release-flatpak-repository"))]}
            return {"path": ".github/workflows/ci.yml", "head_sha": "a" * 40, "event": "workflow_dispatch",
                    "head_branch": "main", "status": "completed", "conclusion": "success"}

        def run(self, *args):
            directory = Path(args[args.index("--dir") + 1])
            (directory / "release-provenance.json").write_text(json.dumps(metadata()))

    assert candidates.inspect(CI(), "123", "a" * 40, "v1.2.3", status_file=tmp_path / "status.json") == "1,2,3,4,5,6,7,8,9"
    with pytest.raises(ValueError, match="exact version"):
        candidates.inspect(CI(), "123", "a" * 40, "v1.2.4", status_file=tmp_path / "status.json")


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


def test_legacy_recovery_clock_uses_green_ci_before_original_release():
    class History:
        repository = "owner/repo"

        def api(self, path):
            if "/workflows/" not in path:
                return {"path": ".github/workflows/release.yml", "created_at": "2026-10-23T12:01:00Z",
                        "updated_at": "2026-10-23T14:30:00Z"}
            base = {"head_sha": "a" * 40, "event": "push", "status": "completed", "conclusion": "success", "run_attempt": 1}
            return {"workflow_runs": [
                {**base, "id": 3, "updated_at": "2026-10-23T14:29:00Z"},
                {**base, "id": 1, "updated_at": "2026-10-23T11:00:00Z"},
                {**base, "id": 2, "updated_at": "2026-10-23T12:00:00Z"},
                {**base, "id": 4, "head_sha": "b" * 40, "updated_at": "2026-10-23T12:00:30Z"},
            ]}

    clock = candidates.source_clock(History(), "123", "a" * 40)
    assert clock == {"ci_run": "2", "ci_run_attempt": "1", "ci_completed_at": "2026-10-23T12:00:00Z"}


def test_missing_original_ci_clock_does_not_fall_back_to_failed_release_completion():
    class Missing:
        repository = "owner/repo"

        def api(self, path):
            return ({"workflow_runs": []} if "/workflows/" in path else
                    {"path": ".github/workflows/release.yml", "created_at": "2026-10-23T12:01:00Z"})

    with pytest.raises(ValueError, match="green CI"):
        candidates.source_clock(Missing(), "123", "a" * 40)


@pytest.mark.parametrize("field,value", [("head_sha", "b" * 40), ("event", "pull_request"),
                                         ("status", "in_progress"), ("conclusion", "failure")])
def test_candidate_ci_clock_cannot_admit_a_different_or_incomplete_check(field, value):
    class Checked:
        repository = "owner/repo"

        def api(self, path):
            return {"path": ".github/workflows/ci.yml", "head_sha": "a" * 40, "event": "workflow_dispatch",
                    "status": "completed", "conclusion": "success", field: value}

    with pytest.raises(ValueError, match="exact revision"):
        candidates.source_clock(Checked(), "123", "a" * 40)
