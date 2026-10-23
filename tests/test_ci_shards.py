"""Exact parametrized-case coverage and measurements across isolated serial runners."""
from copy import deepcopy
from pathlib import Path
import sys

import pytest

from ci_test_shards import partition, verify

sys.path.insert(0, str(Path(__file__).parents[1] / "packaging"))
from ci_plan import timing_source
from ci_test_metrics import collect, compare, measure


def test_duration_balance_keeps_files_together_and_automatically_assigns_new_cases():
    nodes = ["heavy.py::test_slow[x]", "heavy.py::test_slow[y]",
             "medium.py::test_a", "light.py::test_b", "new.py::test_new[1]", "new.py::test_new[2]"]
    times = {"heavy.py": {"seconds": 100, "cases": 2}, "medium.py": {"seconds": 60, "cases": 1},
             "light.py": {"seconds": 20, "cases": 1}}
    owners, loads = partition(nodes, times)
    assert owners == partition(list(reversed(nodes)), times)[0]
    assert max(loads) == 160
    assert min(loads) == 120
    assert set(owners) == {node.split("::")[0] for node in nodes}
    assert sum(loads) == 280  # unknown cases use measured median per-case time (50s)


def reports():
    original = ["one.py::test_case[1]", "one.py::test_case[2]", "two.py::test_case"]
    owners, loads = partition(original, {})
    rows = []
    for index in (1, 2):
        chosen = [node for node in original if owners[node.split("::")[0]] == index]
        rows.append({"schema": 2, "suite": "unit", "index": index, "total": 2, "revision": "abc",
                     "expected": original, "selected": chosen, "file_owners": owners, "estimated_seconds": loads,
                     "executions": chosen.copy(), "repeated_phases": [],
                     "exitstatus": 0, "elapsed_seconds": 10,
                     "tests": {node: {phase: {"seconds": .1, "outcome": "passed"}
                                      for phase in ("setup", "call", "teardown")} for node in chosen}})
    return rows


def test_every_parameter_case_executes_once_and_all_phases_feed_timings():
    result = verify(reports(), "unit")
    assert result["cases"] == 3
    assert result["files"]["one.py"]["cases"] == 2
    assert result["files"]["one.py"]["seconds"] == pytest.approx(.6)
    assert result["test_runner_minutes"] == pytest.approx(20 / 60)
    assert result["phase_seconds"] == pytest.approx({"setup": .3, "call": .3, "teardown": .3})


def test_serial_control_requires_explicit_total_and_has_identical_case_coverage():
    rows = reports()
    control = deepcopy(rows[0])
    control.update(total=1, selected=control["expected"], executions=control["expected"],
                   file_owners={name: 1 for name in control["file_owners"]},
                   estimated_seconds=[sum(control["estimated_seconds"])])
    control["tests"].update(rows[1]["tests"])
    with pytest.raises(ValueError, match="all 2"):
        verify([control], "unit")
    result = verify([control], "unit", total=1)
    assert result["shards"] == 1
    assert result["case_digest"] == verify(rows, "unit")["case_digest"]


@pytest.mark.parametrize("fault", ["missing", "duplicate", "repeated-case", "repeated-phase", "revision", "collection", "interrupted", "failed-phase", "missing-call", "failed", "absent-shard"])
def test_missing_duplicate_or_incomplete_work_cannot_pass(fault):
    rows = deepcopy(reports())
    if fault == "missing":
        rows[0]["tests"].pop(next(iter(rows[0]["tests"])))
    elif fault == "duplicate":
        node = next(iter(rows[0]["tests"]))
        rows[1]["tests"][node] = rows[0]["tests"][node]
    elif fault == "repeated-case":
        rows[0]["executions"].append(rows[0]["executions"][0])
    elif fault == "repeated-phase":
        rows[0]["repeated_phases"].append([rows[0]["executions"][0], "call"])
    elif fault == "revision":
        rows[1]["revision"] = "different"
    elif fault == "collection":
        rows[1]["expected"] = [*rows[1]["expected"], "new.py::test_case"]
    elif fault == "interrupted":
        next(iter(rows[1]["tests"].values())).pop("teardown")
    elif fault == "failed-phase":
        next(iter(rows[1]["tests"].values()))["call"]["outcome"] = "failed"
    elif fault == "missing-call":
        next(iter(rows[1]["tests"].values())).pop("call")
    elif fault == "failed":
        rows[1]["exitstatus"] = 1
    else:
        rows.pop()
    with pytest.raises(ValueError):
        verify(rows, "unit")


def test_intentional_setup_skips_are_retained_as_completed_cases():
    rows = reports()
    node, phases = next(iter(rows[0]["tests"].items()))
    phases["setup"]["outcome"] = "skipped"
    phases.pop("call")
    result = verify(rows, "unit")
    assert result["cases"] == 3 and result["skipped"] == [node]


def test_all_shards_pin_one_trusted_unexpired_baseline_artifact():
    class Source:
        repository = "owner/repo"

        def api(self, path):
            if "/artifacts?" in path:
                return {"artifacts": [{"name": "source-test-timings-2", "id": 55,
                                       "expired": False, "size_in_bytes": 100}]}
            return {"workflow_runs": [
                {"head_branch": "main", "status": "completed", "conclusion": "success", "event": "pull_request"},
                {"head_branch": "main", "status": "completed", "conclusion": "success", "event": "push",
                 "id": 123, "run_attempt": 2},
            ]}
    assert timing_source(Source()) == {"timing_run": "123", "timing_artifact": "55"}


def hosted_jobs(shards=2, prefix=""):
    jobs = [{"name": f"{suite} ({index}/{shards})", "html_url": "https://example.test/job", "conclusion": "success",
             "created_at": "2026-10-23T12:00:00Z", "started_at": "2026-10-23T12:01:00Z",
             "completed_at": "2026-10-23T12:04:00Z", "steps": [
                 {"name": "Run the test suite", "started_at": "2026-10-23T12:02:00Z", "completed_at": "2026-10-23T12:03:00Z"}]}
            for suite in ("unit", "browser") for index in range(1, shards + 1)]
    jobs.extend([
        {"name": "plan", "html_url": "https://example.test/plan", "conclusion": "success",
         "created_at": "2026-10-23T11:58:00Z", "started_at": "2026-10-23T11:58:00Z",
         "completed_at": "2026-10-23T11:59:00Z"},
        {"name": "coverage", "html_url": "https://example.test/coverage", "conclusion": "success",
         "created_at": "2026-10-23T12:04:00Z", "started_at": "2026-10-23T12:04:10Z",
         "completed_at": "2026-10-23T12:04:30Z"},
        {"name": "checks", "html_url": "https://example.test/checks", "conclusion": "success",
         "created_at": "2026-10-23T12:04:30Z", "started_at": "2026-10-23T12:04:40Z",
         "completed_at": "2026-10-23T12:04:50Z"},
    ])
    for job in jobs:
        job["name"] = prefix + job["name"]
    return jobs


@pytest.mark.parametrize("prefix", ["", "release source checks / "])
def test_hosted_measurement_includes_queue_setup_and_total_runner_minutes(prefix):
    jobs = hosted_jobs(prefix=prefix)
    result = measure(jobs, "abc")
    assert result["complete"] and result["elapsed_seconds"] == 410
    assert result["matrix_elapsed_seconds"] == 240
    assert result["runner_minutes"] == pytest.approx(12 + 90 / 60)
    assert result["test_runner_minutes"] == 12
    assert result["jobs"][0]["setup_seconds"] == 60
    jobs[0]["conclusion"] = "failure"
    failed = measure(jobs, "abc")
    assert not failed["complete"] and failed["failed_or_cancelled_jobs"]
    assert not measure(jobs[:4], "abc")["complete"]


@pytest.mark.parametrize("fault", ["duplicate", "cancelled-before-start", "missing-step", "invalid-clock", "active-gate"])
def test_incomplete_hosted_jobs_retain_diagnostics_without_claiming_a_complete_measurement(fault):
    jobs = hosted_jobs()
    if fault == "duplicate":
        jobs[0] = deepcopy(jobs[1])
    elif fault == "cancelled-before-start":
        jobs[0].update(conclusion="cancelled", started_at=None, completed_at=None, steps=[])
    elif fault == "missing-step":
        jobs[0]["steps"] = []
    elif fault == "invalid-clock":
        jobs[0]["created_at"] = "2026-10-23T12:05:00Z"
    else:
        jobs[-1].update(conclusion=None, completed_at=None)
    result = measure(jobs, "abc")
    assert not result["complete"]
    if fault == "cancelled-before-start":
        assert result["failed_or_cancelled_jobs"] == [jobs[0]["html_url"]]
        assert result["jobs"][0]["queue_seconds"] is None


@pytest.mark.parametrize("event,head,accepted", [
    ("pull_request", "branch-sha", True),
    ("workflow_dispatch", "branch-sha", False),
    ("workflow_dispatch", "merge-sha", True),
])
def test_hosted_collection_paginates_only_the_requested_attempt_and_uses_its_actual_revision(event, head, accepted):
    class Source:
        repository = "owner/repo"

        def __init__(self):
            self.paths = []

        def api(self, path):
            self.paths.append(path)
            if "/jobs?" not in path:
                return {"head_sha": head, "event": event, "run_attempt": 3,
                        "html_url": "https://example.test/run/123"}
            other = {**hosted_jobs()[0], "name": "containers / smoke"}
            return {"jobs": hosted_jobs() + [other] * 93 if path.endswith("&page=1") else [other]}

    source = Source()
    suites = {suite: {**verify(reports(), "unit"), "suite": suite, "revision": "merge-sha"}
              for suite in ("unit", "browser")}
    result = collect(source, "123", None, 2, suites)
    assert result["complete"] == accepted
    assert result["revision"] == "merge-sha"
    assert result["head_revision"] == head
    assert result["run_attempt"] == "3"
    assert len(source.paths) == 3
    assert all("/attempts/3/jobs?" in path for path in source.paths[1:])


def measurements():
    rows = []
    for total, elapsed, cost in ((1, 400, 12), (1, 440, 14), (2, 250, 15), (2, 230, 17)):
        row = measure(hosted_jobs(total), "abc", shards=total)
        row.update(run_id=str(len(rows) + 1), run_attempt="1", url=f"https://example.test/run/{len(rows) + 1}",
                   elapsed_seconds=elapsed, runner_minutes=cost,
                   suites={suite: {**verify(reports(), "unit"), "suite": suite, "shards": total}
                           for suite in ("unit", "browser")})
        rows.append(row)
    return rows


def test_repeated_same_revision_comparison_reports_variance_setup_and_fixture_cost():
    result = compare(measurements())
    assert result["wall_time_reduction_percent"] == pytest.approx(100 * (1 - 240 / 420))
    assert result["runner_minute_change"] == 3
    assert result["measurements"]["1"]["elapsed_range_seconds"] == [400, 440]
    assert result["measurements"]["2"]["median_setup_seconds"] == 240
    assert result["measurements"]["2"]["median_phase_seconds"]["setup"] == pytest.approx(.6)


@pytest.mark.parametrize("fault", ["single-run", "revision", "duplicate", "cases", "failed", "missing-suite", "stale-timings"])
def test_comparison_cannot_hide_unmatched_or_incomplete_evidence(fault):
    rows = measurements()
    if fault == "single-run":
        rows.pop()
    elif fault == "revision":
        rows[0]["revision"] = "different"
    elif fault == "duplicate":
        rows[0].update(run_id=rows[1]["run_id"], run_attempt=rows[1]["run_attempt"])
    elif fault == "cases":
        rows[0]["suites"]["browser"]["case_digest"] = "different"
    elif fault == "failed":
        rows[0]["complete"] = False
    elif fault == "missing-suite":
        rows[0]["suites"].pop("browser")
    else:
        rows[0]["suites"]["unit"]["shards"] = 2
    with pytest.raises(ValueError):
        compare(rows)
