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
        assert "push" not in definition["on"]
        assert set(definition["on"]) == {"pull_request"}
        concurrency = definition["concurrency"]
        assert "github.event_name" in concurrency["group"]
        assert "github.event.pull_request.number || github.ref" in concurrency["group"]
        assert concurrency["cancel-in-progress"] == "true"
    assert workflow("release")["on"]["push"] == {"tags": ["v*"]}
    assert set(workflow("release")["on"]["workflow_dispatch"]["inputs"]) == {"tag", "source_run"}
    assert "concurrency" not in workflow("release")
    assert set(workflow("container")["on"]) == {"workflow_dispatch", "workflow_call"}


def test_only_coordinator_can_publish_and_every_required_platform_blocks_promotion():
    release = workflow("release")
    promote = release["jobs"]["promote"]
    assert set(promote["needs"]) == {"version", "preflight", "candidates"}
    for name in promote["needs"]:
        assert f"needs.{name}.result == 'success'" in promote["if"]
    candidates = release["jobs"]["candidates"]
    assert set(candidates["needs"]) == {"version", "prepared"}
    assert "needs.prepared.result == 'success'" in candidates["if"]
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
    assert "push" not in ci["on"]
    assert "pull_request" in ci["on"]


def test_publication_retry_checks_the_original_builds_and_reuses_their_artifacts():
    release = workflow("release")
    inputs = release["on"]["workflow_dispatch"]["inputs"]
    assert inputs["tag"]["required"] == "true"
    assert inputs["source_run"]["required"] == "false"
    jobs = release["jobs"]
    for name in ("wheel", "flatpak", "macos", "containers"):
        assert name not in jobs
    checkout = jobs["version"]["steps"][0]
    assert checkout["with"]["ref"] == "${{ inputs.tag || github.ref }}"
    verify = next(step for step in jobs["candidates"]["steps"]
                  if "release_candidates.py" in step.get("run", ""))
    assert verify["env"]["SOURCE_RUN"] == "${{ inputs.source_run || github.run_id }}"
    assert verify["env"]["RELEASE_TAG"] == "${{ needs.version.outputs.tag }}"
    assert verify["env"]["RELEASE_REVISION"] == "${{ needs.version.outputs.revision }}"
    download = next(step for step in jobs["promote"]["steps"]
                    if step.get("uses", "").startswith("actions/download-artifact"))
    assert download["with"]["run-id"] == "${{ needs.candidates.outputs.source_run }}"
    assert download["with"]["artifact-ids"] == "${{ needs.candidates.outputs.artifact_ids }}"
    assert jobs["promote"]["concurrency"]["group"] == "release-production"


def test_containers_smoke_only_on_prs_and_never_rebuild_for_publication():
    steps = workflow("container")["jobs"]["build"]["steps"]
    builds = [i for i, s in enumerate(steps) if s.get("uses", "").startswith("docker/build-push-action")]
    smoke = next(i for i, s in enumerate(steps) if "packaging/container/smoke.py" in s.get("run", ""))
    stage = next(i for i, s in enumerate(steps) if "docker push" in s.get("run", ""))
    assert len(builds) == 1 and builds[0] < smoke < stage
    assert steps[smoke]["if"] == "github.event_name == 'pull_request'"
    assert "docker inspect" in steps[stage]["run"]
    assert steps[builds[0]]["with"].get("push") != "true"


def test_all_platform_builds_use_the_coordinator_normalized_version():
    jobs = workflow("prepared-candidates")["jobs"]
    for name in ("containers", "macos"):
        assert jobs[name]["with"]["version"] == "${{ inputs.version }}"
    assert jobs["wheel"]["env"]["SETUPTOOLS_SCM_PRETEND_VERSION"] == "${{ inputs.version }}"
    assert jobs["flatpak"]["needs"] == "wheel"
    assert jobs["macos"]["with"]["tag"] == "${{ inputs.tag }}"


def test_pr_packaging_waits_for_source_checks_and_release_builds_are_independent():
    jobs = workflow("ci")["jobs"]
    assert jobs["checks"]["needs"] == ["plan", "unit", "browser", "coverage"]
    assert jobs["checks"]["if"] == "always()"
    for name in ("containers", "macos", "wheel", "flatpak"):
        assert "checks" in jobs[name]["needs"]
        assert jobs[name]["if"] == "github.event_name == 'pull_request'"
    assert "prepared" not in jobs and "release-ready" not in jobs
    release = workflow("release")["jobs"]
    assert "checks" not in release
    assert release["preflight"]["needs"] == "version"
    prepared = release["prepared"]
    assert prepared["needs"] == ["version", "preflight"]
    assert prepared["uses"] == "./.github/workflows/prepared-candidates.yml"
    assert prepared["if"] == "${{ !inputs.source_run }}"
    assert prepared["with"]["revision"] == "${{ needs.version.outputs.revision }}"
    for name in ("containers", "macos", "flatpak"):
        assert workflow("prepared-candidates")["jobs"][name]["with"]["revision"] == "${{ inputs.revision }}"
    # PRs use their merge revision; release builders receive the pinned tag revision.
    for name in ("container", "macos", "flatpak"):
        for job in workflow(name)["jobs"].values():
            for step in job.get("steps", []):
                if step.get("uses", "").startswith("actions/checkout@"):
                    assert step["with"]["ref"] == "${{ inputs.revision || github.sha }}"


def test_publisher_rehearsal_waits_for_pr_checks():
    jobs = workflow("ci")["jobs"]
    condition = jobs["publisher-contract"]["if"]
    assert "!cancelled()" in condition
    assert "needs.checks.result == 'success'" in condition


@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped"])
def test_source_check_aggregate_rejects_every_non_success(result):
    script = workflow("ci")["jobs"]["checks"]["steps"][0]["run"]
    completed = subprocess.run(
        ["bash", "-e", "-c", script], capture_output=True,
        env={**os.environ, "RESULTS": json.dumps({"plan": {"result": "success"},
                                                "unit": {"result": "success"},
                                                "coverage": {"result": "success"},
                                                "browser": {"result": result}}),
             "GITHUB_EVENT_NAME": "pull_request"},
    )
    assert (completed.returncode == 0) == (result == "success")


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
        smoke = next(step for step in job["steps"] if "smoke." in step.get("run", ""))
        assert smoke["if"] == "github.event_name == 'pull_request'"
    smoke = next(step for step in workflow("container")["jobs"]["build"]["steps"]
                 if "packaging/container/smoke.py" in step.get("run", ""))
    assert smoke["if"] == "github.event_name == 'pull_request'"
    assert workflow("ci")["permissions"] == {"contents": "read"}
    assert "secrets" not in workflow("ci")["jobs"]["flatpak"]
    assert "permissions" not in workflow("container")["jobs"]["build"]


def test_publisher_rehearsal_is_trusted_isolated_and_cleans_up_after_failure():
    job = workflow("ci")["jobs"]["publisher-contract"]
    assert job["needs"] == "checks"
    assert job["permissions"] == {"contents": "write", "packages": "write"}
    assert "github.event_name == 'pull_request'" in job["if"]
    assert "github.event.pull_request.head.repo.full_name == github.repository" in job["if"]
    assert "github.event_name == 'push'" not in job["if"]
    assert any(step.get("run") == "python3 packaging/release_services.py registry"
               for step in job["steps"])
    assert job["steps"][-1]["if"] == "always()"
    assert "--cleanup-only" in job["steps"][-1]["run"]
    preflight = workflow("release")["jobs"]["preflight"]
    assert preflight["permissions"] == {"contents": "write", "packages": "write"}
    assert all("rehearse_publisher.py" not in step.get("run", "")
               and "--rehearsal-tag" not in step.get("run", "")
               for step in preflight["steps"])
    assert "preflight" not in workflow("prepared-candidates")["jobs"]
    promotion_steps = workflow("release")["jobs"]["promote"]["steps"]
    promote = next(i for i, s in enumerate(promotion_steps) if "packaging/promote_release.py" in s.get("run", ""))
    public = next(i for i, s in enumerate(promotion_steps) if "release_services.py public" in s.get("run", ""))
    assert promote < public


def test_publication_retry_reuses_validated_artifacts_without_platform_builds():
    jobs = workflow("release")["jobs"]
    for name in ("containers", "macos", "wheel", "flatpak"):
        assert name not in jobs
    assert jobs["prepared"]["if"] == "${{ !inputs.source_run }}"
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
    assert timing["if"] == "needs.candidates.outputs.release_started_at != ''"


def test_serial_shards_keep_minimum_runtime_resolution_checks_and_diagnostics():
    jobs = workflow("ci")["jobs"]
    for name in ("unit", "browser"):
        job = jobs[name]
        assert job["strategy"]["fail-fast"] == "false"
        assert job["strategy"]["matrix"]["shard"] == ["1", "2", "3", "4"]
        assert job["env"]["SHARD_TOTAL"] == "4"
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


def test_tests_and_measurements_can_only_run_on_prs():
    definition = workflow("ci")
    assert set(definition["on"]) == {"pull_request"}
    jobs = definition["jobs"]
    for name in ("unit", "browser", "coverage", "metrics", "publisher-contract",
                 "containers", "macos", "flatpak"):
        assert "github.event_name == 'pull_request'" in jobs[name]["if"]
    assert jobs["metrics"]["needs"] == ["plan", "coverage", "checks"]
    assert "always()" in jobs["metrics"]["if"]
    assert all(' --total="$SHARD_TOTAL"' in line for line in jobs["coverage"]["steps"][-2]["run"].splitlines())
    # The reporting job must never become a prerequisite of the gate it measures.
    assert "metrics" not in jobs["checks"]["needs"]


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_aggregate_cannot_ignore_failed_or_missing_shard_coverage(result):
    script = workflow("ci")["jobs"]["checks"]["steps"][0]["run"]
    completed = subprocess.run(["bash", "-e", "-c", script], capture_output=True, env={
        **os.environ, "GITHUB_EVENT_NAME": "pull_request",
        "RESULTS": json.dumps({name: {"result": result if name == "coverage" else "success"}
                               for name in ("plan", "unit", "browser", "coverage")}),
    })
    assert completed.returncode != 0
