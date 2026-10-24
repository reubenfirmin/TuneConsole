"""Enforce the publication boundary across the reusable workflow graph."""
from pathlib import Path
import json
import os
import subprocess

import pytest

import yaml

ROOT = Path(__file__).parents[1]


def workflow(name):
    return yaml.load((ROOT / f".github/workflows/{name}.yml").read_text(), Loader=yaml.BaseLoader)


def test_ordinary_ci_has_one_event_owner_and_cannot_cancel_tag_releases():
    for name in ("ci",):
        definition = workflow(name)
        assert definition["on"]["push"] == {"branches": ["main"]}
        assert "pull_request" in definition["on"]
        assert "workflow_dispatch" in definition["on"]
        assert "workflow_call" in definition["on"]
        concurrency = definition["concurrency"]
        assert "github.event_name" in concurrency["group"]
        assert "github.event.pull_request.number || github.ref" in concurrency["group"]
        assert concurrency["cancel-in-progress"] == (
            "${{ github.event_name != 'push' || !startsWith(github.ref, 'refs/tags/') }}"
        )
    assert workflow("release")["on"]["push"] == {"tags": ["v*"]}
    assert set(workflow("release")["on"]["workflow_dispatch"]["inputs"]) == {"tag", "source_run"}
    assert "concurrency" not in workflow("release")
    assert set(workflow("container")["on"]) == {"workflow_dispatch", "workflow_call"}


def test_only_coordinator_can_publish_and_every_required_platform_blocks_promotion():
    release = workflow("release")
    promote = release["jobs"]["promote"]
    assert set(promote["needs"]) == {"version", "checks", "preflight", "candidates"}
    for name in promote["needs"]:
        assert f"needs.{name}.result == 'success'" in promote["if"]
    candidates = release["jobs"]["candidates"]
    assert set(candidates["needs"]) == {"version", "checks"}
    assert set(workflow("prepared-candidates")["jobs"]["provenance"]["needs"]) == {"wheel", "flatpak", "macos", "containers"}
    assert promote["concurrency"] == {"group": "release-production", "cancel-in-progress": "false"}
    for name in ("macos", "container", "flatpak"):
        definition = workflow(name)
        assert "workflow_call" in definition["on"]
        assert "tags" not in definition["on"].get("push", {})
        content = (ROOT / f".github/workflows/{name}.yml").read_text()
        assert "action-gh-release" not in content
        assert "pages deploy" not in content and ":latest" not in content


def test_release_does_not_restart_unit_or_browser_suites():
    jobs = workflow("release")["jobs"]
    assert all(job.get("uses") != "./.github/workflows/ci.yml" for job in jobs.values())
    assert all("pytest" not in step.get("run", "")
               for job in jobs.values() for step in job.get("steps", []))
    ci = workflow("ci")
    assert ci["on"]["push"] == {"branches": ["main"]}
    assert "pull_request" in ci["on"]


def test_publication_retry_checks_the_original_builds_and_reuses_their_artifacts():
    release = workflow("release")
    inputs = release["on"]["workflow_dispatch"]["inputs"]
    assert inputs["tag"]["required"] == "true"
    assert inputs["source_run"]["required"] == "true"
    jobs = release["jobs"]
    for name in ("wheel", "flatpak", "macos", "containers"):
        assert name not in jobs
    checkout = jobs["version"]["steps"][0]
    assert checkout["with"]["ref"] == "${{ inputs.tag || github.ref }}"
    verify = next(step for step in jobs["candidates"]["steps"]
                  if "release_candidates.py" in step.get("run", ""))
    assert verify["env"]["SOURCE_RUN"] == "${{ inputs.source_run || 'auto' }}"
    assert verify["env"]["RELEASE_TAG"] == "${{ needs.version.outputs.tag }}"
    assert verify["env"]["RELEASE_REVISION"] == "${{ needs.version.outputs.revision }}"
    download = next(step for step in jobs["promote"]["steps"]
                    if step.get("uses", "").startswith("actions/download-artifact"))
    assert download["with"]["run-id"] == "${{ needs.candidates.outputs.source_run }}"
    assert download["with"]["artifact-ids"] == "${{ needs.candidates.outputs.artifact_ids }}"
    assert jobs["promote"]["concurrency"]["group"] == "release-production"


def test_containers_stage_the_image_only_after_smoke_and_never_rebuild_for_publication():
    steps = workflow("container")["jobs"]["smoke"]["steps"]
    builds = [i for i, s in enumerate(steps) if s.get("uses", "").startswith("docker/build-push-action")]
    smoke = next(i for i, s in enumerate(steps) if "packaging/container/smoke.py" in s.get("run", ""))
    stage = next(i for i, s in enumerate(steps) if "docker push" in s.get("run", ""))
    assert len(builds) == 1 and builds[0] < smoke < stage
    assert "docker inspect" in steps[stage]["run"]
    assert steps[builds[0]]["with"].get("push") != "true"


def test_all_platform_builds_use_the_coordinator_normalized_version():
    jobs = workflow("prepared-candidates")["jobs"]
    for name in ("containers", "macos"):
        assert jobs[name]["with"]["version"] == "${{ inputs.version }}"
    assert jobs["wheel"]["env"]["SETUPTOOLS_SCM_PRETEND_VERSION"] == "${{ inputs.version }}"
    assert jobs["flatpak"]["needs"] == ["preflight", "wheel"]
    assert jobs["macos"]["with"]["tag"] == "${{ inputs.tag }}"


def test_packaging_waits_for_all_source_checks_on_the_same_workflow_revision():
    jobs = workflow("ci")["jobs"]
    assert jobs["checks"]["needs"] == ["plan", "unit", "browser", "coverage"]
    assert jobs["checks"]["if"] == "always()"
    for name in ("containers", "macos", "wheel", "flatpak"):
        assert "checks" in jobs[name]["needs"]
        assert jobs[name]["if"] == "inputs.release_tag == ''"
    for name in ("containers", "macos", "wheel", "flatpak"):
        assert "preflight" in workflow("prepared-candidates")["jobs"][name]["needs"]
    assert jobs["prepared"]["needs"] == ["plan", "checks"]
    assert "github.ref == 'refs/heads/main'" in jobs["prepared"]["if"]
    assert "github.event_name == 'workflow_dispatch'" in jobs["prepared"]["if"]
    assert "uses" not in workflow("release")["jobs"]["checks"]
    # Called workflows check out the caller's revision, never a default-branch override.
    for name in ("container", "macos", "flatpak"):
        for job in workflow(name)["jobs"].values():
            for step in job.get("steps", []):
                if step.get("uses", "").startswith("actions/checkout@"):
                    assert "ref" not in step.get("with", {})


def test_release_preparation_survives_reused_suites_without_bypassing_failed_checks():
    jobs = workflow("ci")["jobs"]
    for name in ("prepared", "publisher-contract"):
        # A plain condition implicitly adds success(), which propagates skipped
        # unit/browser ancestors despite the checks gate successfully admitting reuse.
        condition = jobs[name]["if"]
        assert "!cancelled()" in condition
        assert "needs.checks.result == 'success'" in condition
    assert "needs.plan.result == 'success'" in jobs["prepared"]["if"]


@pytest.mark.parametrize("plan,checks,prepared,accepted", [
    ("success", "success", "success", True),
    ("failure", "skipped", "skipped", False),
    ("success", "failure", "skipped", False),
    ("success", "success", "skipped", False),
    ("success", "success", "failure", False),
    ("success", "success", "cancelled", False),
])
def test_release_readiness_rejects_missing_or_failed_preparation(plan, checks, prepared, accepted):
    job = workflow("ci")["jobs"]["release-ready"]
    assert job["needs"] == ["plan", "checks", "prepared"]
    assert "!cancelled()" in job["if"]
    assert "inputs.release_tag != ''" in job["if"]
    script = job["steps"][0]["run"]
    completed = subprocess.run(["bash", "-e", "-c", script], capture_output=True, env={
        **os.environ, "RESULTS": json.dumps({"plan": {"result": plan}, "checks": {"result": checks},
                                            "prepared": {"result": prepared}}),
    })
    assert (completed.returncode == 0) == accepted


@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped"])
def test_source_check_aggregate_rejects_every_non_success(result):
    script = workflow("ci")["jobs"]["checks"]["steps"][0]["run"]
    completed = subprocess.run(
        ["bash", "-e", "-c", script], capture_output=True,
        env={**os.environ, "RESULTS": json.dumps({"plan": {"result": "success"},
                                                "unit": {"result": "success"},
                                                "coverage": {"result": "success"},
                                                "browser": {"result": result}}),
             "REUSE_TESTS": "false", "CHECKED_RUN": ""},
    )
    assert (completed.returncode == 0) == (result == "success")


@pytest.mark.parametrize("sha,event,status,conclusion,accepted", [
    ("tagged", "push", "completed", "success", True),
    ("tagged", "workflow_dispatch", "completed", "success", True),
    ("older", "push", "completed", "success", False),
    ("tagged", "pull_request", "completed", "success", False),
    ("tagged", "push", "in_progress", None, False),
    ("tagged", "push", "completed", "failure", False),
    ("tagged", "push", "completed", "cancelled", False),
])
def test_release_gate_requires_prior_success_for_the_tagged_commit(
    tmp_path, sha, event, status, conclusion, accepted,
):
    gh = tmp_path / "gh"
    gh.write_text('#!/bin/sh\ncat "$CI_RUNS"\n')
    gh.chmod(0o755)
    runs = tmp_path / "runs.json"
    runs.write_text(json.dumps({"workflow_runs": [
        {"id": 123, "head_sha": sha, "event": event, "status": status,
         "conclusion": conclusion},
    ]}))
    script = workflow("release")["jobs"]["checks"]["steps"][0]["run"]
    completed = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script], capture_output=True,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "CI_RUNS": str(runs),
             "RELEASE_REVISION": "tagged", "GITHUB_REPOSITORY": "owner/repo"},
    )
    assert (completed.returncode == 0) == accepted


def test_platform_ci_exercises_release_build_and_packaged_smoke_on_both_architectures():
    for name, arches in (("macos", {"arm64", "x86_64"}),
                         ("flatpak", {"aarch64", "x86_64"})):
        job = workflow(name)["jobs"]["build"]
        assert {entry["arch"] for entry in job["strategy"]["matrix"]["include"]} == arches
        commands = "\n".join(step.get("run", "") for step in job["steps"])
        if name == "macos":
            assert "packaging/macos/build.sh" in commands
            assert "packaging/macos/smoke.sh" in commands
        else:
            assert "flatpak-builder" in commands and "--install-deps-from=flathub" in commands
            assert "flatpak install --user" in commands and 'python3 smoke.py "$VERSION"' in commands
    assert workflow("ci")["permissions"] == {"contents": "read"}
    assert "secrets" not in workflow("ci")["jobs"]["flatpak"]
    assert "permissions" not in workflow("container")["jobs"]["smoke"]


def test_publisher_rehearsal_is_trusted_isolated_and_cleans_up_after_failure():
    job = workflow("ci")["jobs"]["publisher-contract"]
    assert job["needs"] == "checks"
    assert job["permissions"] == {"contents": "write", "packages": "write"}
    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert "github.event_name == 'push'" in job["if"]
    assert "pull_request" not in job["if"]
    assert any(step.get("run") == "python3 packaging/release_services.py registry"
               for step in job["steps"])
    assert job["steps"][-1]["if"] == "always()"
    assert "--cleanup-only" in job["steps"][-1]["run"]
    preflight = workflow("release")["jobs"]["preflight"]
    assert preflight["permissions"] == {"contents": "write", "packages": "write"}
    for name in ("containers", "macos", "wheel", "flatpak"):
        assert "preflight" in workflow("prepared-candidates")["jobs"][name]["needs"]
    assert preflight["steps"][-1]["if"] == "always()"
    promotion_steps = workflow("release")["jobs"]["promote"]["steps"]
    promote = next(i for i, s in enumerate(promotion_steps) if "packaging/promote_release.py" in s.get("run", ""))
    public = next(i for i, s in enumerate(promotion_steps) if "release_services.py public" in s.get("run", ""))
    assert promote < public


def test_publication_retry_reuses_validated_artifacts_without_platform_builds():
    jobs = workflow("release")["jobs"]
    for name in ("containers", "macos", "wheel", "flatpak"):
        assert name not in jobs
    download = next(s for s in jobs["promote"]["steps"] if s.get("uses", "").startswith("actions/download-artifact"))
    assert download["with"]["run-id"] == "${{ needs.candidates.outputs.source_run }}"
    assert download["with"]["artifact-ids"] == "${{ needs.candidates.outputs.artifact_ids }}"
    assert jobs["summary"]["if"] == "always()"
    for name in ("flatpak", "macos"):
        steps = workflow(name)["jobs"]["build"]["steps"]
        assert any("ci_stage.py --timeout" in s.get("run", "") for s in steps)
        assert any(s.get("if") == "always()" and "upload-artifact" in s.get("uses", "") for s in steps)


def test_release_timing_reads_only_this_attempts_flattened_candidate_status():
    steps = workflow("release")["jobs"]["summary"]["steps"]
    download = next(step for step in steps if step.get("with", {}).get("path") == "evidence/candidates")
    assert download["with"]["pattern"] == "candidate-status-${{ github.run_attempt }}"
    assert download["with"]["merge-multiple"] == "true"
    timing = next(step for step in steps if "packaging/release_timing.py" in step.get("run", ""))
    assert timing["if"] == "needs.candidates.outputs.ci_completed_at != ''"


@pytest.mark.parametrize("checked_run,unit,browser,accepted", [
    ("123", "skipped", "skipped", True),
    ("", "skipped", "skipped", False),
    ("123", "failure", "skipped", False),
    ("123", "skipped", "cancelled", False),
])
def test_preparation_reuses_only_explicitly_verified_source_checks(checked_run, unit, browser, accepted):
    script = workflow("ci")["jobs"]["checks"]["steps"][0]["run"]
    completed = subprocess.run(["bash", "-e", "-c", script], capture_output=True, env={
        **os.environ, "REUSE_TESTS": "true", "CHECKED_RUN": checked_run,
        "RESULTS": json.dumps({"plan": {"result": "success"}, "unit": {"result": unit}, "browser": {"result": browser},
                               "coverage": {"result": "skipped"}}),
    })
    assert (completed.returncode == 0) == accepted


def test_serial_shards_keep_minimum_runtime_resolution_checks_and_diagnostics():
    jobs = workflow("ci")["jobs"]
    for name in ("unit", "browser"):
        job = jobs[name]
        assert job["strategy"]["fail-fast"] == "false"
        assert job["strategy"]["matrix"]["shard"] == "${{ fromJSON(inputs.test_shards == '1' && '[1]' || inputs.test_shards == '2' && '[1, 2]' || '[1, 2, 3, 4]') }}"
        assert job["env"]["SHARD_TOTAL"] == "${{ inputs.test_shards || '4' }}"
        commands = "\n".join(step.get("run", "") for step in job["steps"])
        assert "--ci-shard=${{ matrix.shard }}/$SHARD_TOTAL" in commands
        assert "--junitxml=evidence/junit.xml" in commands
        assert " -n " not in commands
        artifact = job["steps"][-1]
        assert artifact["if"] == "always()"
        assert "matrix.shard" in artifact["with"]["name"]
        setup = next(step for step in job["steps"] if step.get("uses", "").startswith("astral-sh/setup-uv"))
        assert setup["with"]["enable-cache"] == "false"
        baseline = next(step for step in job["steps"] if step.get("with", {}).get("path") == "shard-baseline")
        assert baseline["with"]["artifact-ids"] == "${{ needs.plan.outputs.timing_artifact }}"
        assert baseline["with"]["merge-multiple"] == "true"
    assert jobs["unit"]["env"]["UV_PYTHON"] == "3.12"
    verification = next(step for step in jobs["unit"]["steps"] if step.get("name") == "Verify packaged resolutions")
    assert verification["if"] == "matrix.shard == 1"
    assert any(step.get("uses", "").startswith("actions/setup-node") for step in jobs["unit"]["steps"])
    assert "--tracing=retain-on-failure" in "\n".join(step.get("run", "") for step in jobs["browser"]["steps"])


def test_browser_image_matches_locked_playwright_and_avoids_per_shard_os_installation():
    import tomllib

    jobs = workflow("ci")["jobs"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    version = next(package["version"] for package in lock["package"] if package["name"] == "playwright")
    assert jobs["browser"]["container"]["image"] == f"mcr.microsoft.com/playwright/python:v{version}-noble"
    assert "--ipc=host" in jobs["browser"]["container"]["options"]
    assert not any("playwright install" in step.get("run", "") for step in jobs["browser"]["steps"])


def test_hosted_baseline_is_opt_in_and_measurements_follow_the_completed_gate():
    definition = workflow("ci")
    choice = definition["on"]["workflow_dispatch"]["inputs"]["test_shards"]
    assert choice["type"] == "choice" and choice["options"] == ["4", "2", "1"] and choice["default"] == "4"
    jobs = definition["jobs"]
    assert jobs["metrics"]["needs"] == ["plan", "coverage", "checks"]
    assert "always()" in jobs["metrics"]["if"]
    assert all(' --total="$SHARD_TOTAL"' in line for line in jobs["coverage"]["steps"][-2]["run"].splitlines())
    # The reporting job must never become a prerequisite of the gate it measures.
    assert "metrics" not in jobs["checks"]["needs"]


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_aggregate_cannot_ignore_failed_or_missing_shard_coverage(result):
    script = workflow("ci")["jobs"]["checks"]["steps"][0]["run"]
    completed = subprocess.run(["bash", "-e", "-c", script], capture_output=True, env={
        **os.environ, "REUSE_TESTS": "false", "CHECKED_RUN": "",
        "RESULTS": json.dumps({name: {"result": result if name == "coverage" else "success"}
                               for name in ("plan", "unit", "browser", "coverage")}),
    })
    assert completed.returncode != 0
