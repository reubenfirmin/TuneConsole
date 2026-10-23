"""Enforce the publication boundary across the reusable workflow graph."""
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def workflow(name):
    return yaml.load((ROOT / f".github/workflows/{name}.yml").read_text(), Loader=yaml.BaseLoader)


def test_only_coordinator_can_publish_and_every_required_platform_blocks_promotion():
    release = workflow("release")
    promote = release["jobs"]["promote"]
    assert set(promote["needs"]) == {"version", "wheel", "flatpak", "macos", "containers"}
    guard = promote["if"]
    assert "!cancelled()" in guard and "needs.version.result == 'success'" in guard
    for name in ("wheel", "flatpak", "macos", "containers"):
        assert f"needs.{name}.result == 'success'" in guard
    assert promote["concurrency"] == {"group": "release-production", "cancel-in-progress": "false"}
    for name in ("macos", "container"):
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
    assert ci["on"]["push"]["tags-ignore"] == ["v*"]
    assert "pull_request" in ci["on"]


def test_publication_retry_checks_the_original_builds_and_reuses_their_artifacts():
    release = workflow("release")
    inputs = release["on"]["workflow_dispatch"]["inputs"]
    assert inputs["candidate_tag"]["required"] == "true"
    assert inputs["candidate_run_id"]["required"] == "true"
    jobs = release["jobs"]
    for name in ("wheel", "flatpak", "macos", "containers"):
        assert jobs[name]["if"] == "github.event_name != 'workflow_dispatch'"
    verify = next(step for step in jobs["version"]["steps"]
                  if "release_candidate.py" in step.get("run", ""))
    assert verify["if"] == "github.event_name == 'workflow_dispatch'"
    assert 'git rev-parse "$CANDIDATE_TAG^{commit}"' in verify["run"]
    download = next(step for step in jobs["promote"]["steps"]
                    if step.get("uses", "").startswith("actions/download-artifact"))
    assert download["with"]["run-id"] == "${{ inputs.candidate_run_id || github.run_id }}"
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
    jobs = workflow("release")["jobs"]
    for name in ("containers", "macos"):
        assert jobs[name]["with"]["version"] == "${{ needs.version.outputs.version }}"
    assert jobs["wheel"]["env"]["SETUPTOOLS_SCM_PRETEND_VERSION"] == "${{ needs.version.outputs.version }}"
    assert jobs["flatpak"]["needs"] == ["version", "wheel"]
    assert jobs["macos"]["with"]["tag"] == "${{ github.ref_name }}"
