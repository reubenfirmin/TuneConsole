"""Release evidence cannot hide queueing, failed verification or missing rehearsals."""
import copy
from datetime import datetime, timezone
import importlib
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "packaging"))
timing = importlib.import_module("release_timing")

RELEASE_STARTED = "2026-10-23T12:00:00Z"


def promotion():
    return {"name": "promote", "status": "completed", "conclusion": "success",
            "created_at": "2026-10-23T12:01:00Z", "started_at": "2026-10-23T12:01:20Z", "steps": [
                {"name": "Install publication tools within a separate setup budget", "status": "completed",
                 "conclusion": "success", "started_at": "2026-10-23T12:01:30Z", "completed_at": "2026-10-23T12:02:30Z"},
                {"name": timing.VERIFY_STEP, "status": "completed", "conclusion": "success",
                 "started_at": "2026-10-23T12:09:00Z", "completed_at": "2026-10-23T12:10:00Z"},
            ]}


def candidate():
    return {"tag": "v1.2.3", "revision": "a" * 40, "source_run": "123", "repository": "owner/repo",
            "release_started_at": RELEASE_STARTED,
            "assets": [{"name": name, "state": "complete", "bytes": 100}
                       for name in timing.EXPECTED]}


def test_pending_production_owner_reports_queue_and_elapsed_since_release_request():
    job = promotion()
    job.update(status="queued", conclusion=None, started_at=None, steps=[])
    report = timing.measure([job], RELEASE_STARTED, now=datetime(2026, 10, 23, 12, 12, tzinfo=timezone.utc))
    assert report["blocking_stage"] == "promote runner queue"
    assert report["observed_elapsed_seconds"] == 720
    assert report["stages"][0]["elapsed_seconds"] == 60
    assert report["stages"][1]["elapsed_seconds"] == 660
    assert not report["within_budget"]


def test_failure_before_any_step_still_identifies_the_blocking_job():
    report = timing.measure([{"name": "candidates", "conclusion": "failure", "steps": []}], RELEASE_STARTED)
    assert report["blocking_stage"] == "candidates"
    assert "candidates" in timing.render(report)


def test_running_step_reports_time_spent_and_summary_cannot_replace_the_blocker():
    job = promotion()
    job["steps"] = [dict(job["steps"][0], status="in_progress", conclusion=None, completed_at=None)]
    report = timing.measure([job, {"name": "summary", "conclusion": "failure"}], RELEASE_STARTED,
                            now=datetime(2026, 10, 23, 12, 3, tzinfo=timezone.utc))
    assert report["blocking_stage"] == "promote / Install publication tools within a separate setup budget"
    assert report["stages"][-1]["elapsed_seconds"] == 90


def test_only_one_promotion_attempt_can_prove_public_verification():
    job = promotion()
    assert not timing.measure([job, job], RELEASE_STARTED)["within_budget"]
    job["name"] = "preflight"
    assert not timing.measure([job], RELEASE_STARTED)["within_budget"]


def test_invalid_verification_clock_cannot_claim_success():
    job = promotion()
    job["steps"][-1]["completed_at"] = "2026-10-23T12:08:00Z"
    assert not timing.measure([job], RELEASE_STARTED)["within_budget"]


class Hosted:
    repository = "owner/repo"

    def __init__(self):
        self.paths = []

    def api(self, path):
        self.paths.append(path)
        assert "/attempts/2/jobs?" in path
        # All jobs matter even if the promotion is on a later page.
        return {"jobs": [{"name": "summary"}] * 100 if path.endswith("page=1") else [promotion()]}


def test_hosted_collection_pins_attempt_paginates_and_preserves_source_and_sizes():
    backend = Hosted()
    report = timing.collect(backend, "456", "2", candidate())
    assert len(backend.paths) == 2
    assert report["within_budget"] and report["elapsed_seconds"] == 600
    assert report["artifact_bytes"] == 900
    assert report["candidate"]["source_run"] == "123"
    assert report["url"].endswith("/456/attempts/2")


def records():
    return [timing.collect(Hosted(), str(run), "2", candidate()) for run in (456, 457, 458)]


def test_three_hosted_records_keep_source_links_sizes_and_reviewed_cold_setup():
    result = timing.compare(records(), cold_run="456")
    assert len(result["runs"]) == 3
    assert all(row["elapsed_seconds"] == 600 and row["artifact_bytes"] == 900 for row in result["runs"])
    assert result["runs"][0]["source"].endswith("/123")
    assert result["cold_setup_run"].endswith("/456/attempts/2")


def test_repeated_input_or_fewer_than_three_runs_cannot_establish_the_target():
    rows = records()
    for incomplete in (rows[:2], [rows[0], rows[0], rows[2]]):
        with pytest.raises(ValueError, match="Three distinct"):
            timing.compare(incomplete, cold_run="456")


@pytest.mark.parametrize("failure", ["late", "failed", "clock", "sizes", "missing_architecture", "expired"])
def test_rehearsal_evidence_rejects_late_failed_or_incomplete_publication(failure):
    rows = records()
    broken = rows[-1]
    if failure == "late":
        broken.update(verified_at="2026-10-23T12:16:00Z", elapsed_seconds=960)
    elif failure == "failed":
        broken.update(verified_at=None, elapsed_seconds=None)
    elif failure == "clock":
        broken["candidate"]["release_started_at"] = "2026-10-23T12:01:00Z"
    elif failure == "sizes":
        broken["artifact_bytes"] += 1
    elif failure == "missing_architecture":
        broken["candidate"]["assets"].pop()
    else:
        broken["candidate"]["assets"][-1]["state"] = "expired"
    with pytest.raises(ValueError, match="complete candidates"):
        timing.compare(rows, cold_run="456")


def test_cold_setup_needs_an_identified_run_with_successful_tool_installation():
    rows = records()
    with pytest.raises(ValueError, match="cold-setup"):
        timing.compare(rows, cold_run="unknown")
    rows = copy.deepcopy(rows)
    rows[0]["stages"] = []
    with pytest.raises(ValueError, match="cold-setup"):
        timing.compare(rows, cold_run="456")
