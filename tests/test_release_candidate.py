"""Only completed, successful builds for the exact release tag can be republished."""
from copy import deepcopy
from pathlib import Path
import runpy

import pytest


candidate = runpy.run_path(Path(__file__).parents[1] / "packaging/release_candidate.py")


@pytest.fixture
def source():
    run = {"path": ".github/workflows/release.yml", "event": "push",
           "head_branch": "v0.1.16", "head_sha": "abc123", "status": "completed"}
    jobs = [{"name": name, "conclusion": "success"} for name in candidate["REQUIRED_JOBS"]]
    jobs.append({"name": "promote", "conclusion": "failure"})
    return run, jobs


def test_successful_candidate_can_resume_after_publication_failed(source):
    candidate["validate"](*source, "v0.1.16", "abc123")


@pytest.mark.parametrize("field,value", [
    ("path", ".github/workflows/ci.yml"), ("event", "pull_request"),
    ("head_branch", "v0.1.15"), ("head_sha", "different"),
    ("status", "in_progress"),
])
def test_unrelated_or_running_candidate_is_rejected(source, field, value):
    run, jobs = deepcopy(source)
    run[field] = value
    with pytest.raises(ValueError, match="exact tag commit"):
        candidate["validate"](run, jobs, "v0.1.16", "abc123")


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", None])
def test_every_platform_build_must_have_succeeded(source, conclusion):
    run, jobs = deepcopy(source)
    jobs[0]["conclusion"] = conclusion
    with pytest.raises(ValueError, match="must have passed"):
        candidate["validate"](run, jobs, "v0.1.16", "abc123")


def test_missing_platform_build_is_rejected(source):
    run, jobs = source
    with pytest.raises(ValueError, match="must have passed"):
        candidate["validate"](run, jobs[1:], "v0.1.16", "abc123")
