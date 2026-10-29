"""Exact parametrized-case coverage and measurements across isolated serial runners."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from ci_test_shards import BASELINE, partition, pytest_configure as configure_shard, verify

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


@pytest.mark.parametrize("suite", ["unit", "browser"])
def test_retained_measured_timings_balance_four_whole_file_groups(suite):
    baseline = json.loads(BASELINE.read_text())
    timings = baseline[suite]
    nodes = [f"{name}::recorded_case[{case}]" for name, row in timings.items() for case in range(row["cases"])]
    owners, loads = partition(nodes, timings, 4)
    assert set(owners.values()) == {1, 2, 3, 4}
    assert sum(loads) == pytest.approx(sum(row["seconds"] for row in timings.values()))
    assert max(loads) - min(loads) < 2
    assert baseline["run"].endswith("/40050066995")
    assert baseline["failed_cases"]  # Diagnostic timing input must not claim a passing source run.
    assert baseline['refresh_run'].endswith('/40193589708')


def reports(total=2, nodeids=None):
    original = nodeids or ["one.py::test_case[1]", "one.py::test_case[2]", "two.py::test_case"]
    owners, loads = partition(original, {}, total)
    rows = []
    for index in range(1, total + 1):
        chosen = [node for node in original if owners[node.split("::")[0]] == index]
        rows.append({"schema": 2, "suite": "unit", "index": index, "total": total, "revision": "abc",
                     "expected": original, "selected": chosen, "file_owners": owners, "estimated_seconds": loads,
                     "executions": chosen.copy(), "repeated_phases": [],
                     "exitstatus": 0, "elapsed_seconds": 10,
                     "tests": {node: {phase: {"seconds": .1, "outcome": "passed"}
                                      for phase in ("setup", "call", "teardown")} for node in chosen}})
    return rows


def test_four_shards_cover_every_parameter_once_and_keep_modules_together():
    nodeids = [f"file-{file}.py::test_case[{case}]" for file in range(12) for case in range(3)]
    rows = reports(4, nodeids)
    assert verify(rows, "unit", total=4)["case_digest"] == verify(reports(1, nodeids), "unit", total=1)["case_digest"]
    assert all(len(row["selected"]) == 9 for row in rows)
    assert set(rows[0]["file_owners"].values()) == {1, 2, 3, 4}
    with pytest.raises(ValueError, match="all 4"):
        verify(rows[:3], "unit", total=4)
    rows[3]["exitstatus"] = 1
    with pytest.raises(ValueError, match="did not pass"):
        verify(rows, "unit", total=4)


@pytest.mark.parametrize("shard", ["1/1", "2/2", "1/4", "4/4", "0/4", "5/4", "1/3", "1/8"])
def test_runner_configuration_accepts_supported_splits_and_rejects_invalid_indices(tmp_path, shard):
    options = {"ci_shard": shard, "ci_shard_report": tmp_path / "shard.json",
               "ci_suite": "unit", "ci_duration_baseline": BASELINE}
    registered = []
    config = SimpleNamespace(getoption=options.__getitem__, pluginmanager=SimpleNamespace(
        register=lambda plugin, name: registered.append(plugin)))
    if shard in {"0/4", "5/4", "1/3", "1/8"}:
        with pytest.raises(pytest.UsageError, match="INDEX/TOTAL"):
            configure_shard(config)
    else:
        configure_shard(config)
        assert registered[0].report["index"] == int(shard.split("/")[0])
        assert registered[0].report["total"] == int(shard.split("/")[1])


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
                {"status": "completed", "conclusion": "success", "event": "push"},
                {"status": "completed", "conclusion": "success", "event": "pull_request",
                 "head_repository": {"full_name": "untrusted/fork"}},
                {"status": "completed", "conclusion": "success", "event": "pull_request",
                 "head_repository": {"full_name": "owner/repo"},
                 "id": 123, "run_attempt": 2, 'run_started_at': '2026-10-26T12:00:00Z'},
            ]}
    assert timing_source(Source()) == {"timing_run": "123", "timing_artifact": "55"}


@pytest.mark.parametrize('started,expected', [
    ('2026-10-24T12:00:00Z', False),
    ('2026-10-25T12:33:40Z', False),
    (None, False),
    ('2026-10-25T12:33:41Z', True),
])
def test_newer_committed_weight_refresh_is_not_overridden_by_stale_pr(started, expected):
    class Source:
        repository = 'owner/repo'

        def api(self, path):
            if '/artifacts?' in path:
                return {'artifacts': [{'name': 'source-test-timings-1', 'id': 99,
                                       'expired': False, 'size_in_bytes': 100}]}
            return {'workflow_runs': [
                {'status': 'completed', 'conclusion': 'success', 'event': 'pull_request',
                 'head_repository': {'full_name': 'owner/repo'},
                 'id': 123, 'run_attempt': 1, 'run_started_at': started},
            ]}

    assert timing_source(Source(), {'refreshed_at': '2026-10-25T12:33:40Z'}) == (
        {'timing_run': '123', 'timing_artifact': '99'} if expected
        else {'timing_run': '', 'timing_artifact': ''})


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


def test_four_runner_metrics_require_every_shard_and_count_all_runner_minutes():
    jobs = hosted_jobs(4)
    result = measure(jobs, "abc", shards=4)
    assert result["complete"]
    assert result["test_runner_minutes"] == 24
    assert len(result["jobs"]) == 8
    assert not measure([job for job in jobs if job["name"] != "browser (4/4)"], "abc", shards=4)["complete"]
    jobs[3]["conclusion"] = "cancelled"
    assert not measure(jobs, "abc", shards=4)["complete"]


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


@pytest.mark.parametrize("baseline", [1, 2])
def test_four_shards_can_be_compared_with_repeated_serial_or_two_shard_controls(baseline):
    rows = measurements()
    for row in rows:
        total = baseline if row["shards"] == 1 else 4
        row["shards"] = total
        for report in row["suites"].values():
            report["shards"] = total
    result = compare(rows)
    assert result["baseline_shards"] == baseline
    assert result["candidate_shards"] == 4
    assert set(result["measurements"]) == {str(baseline), "4"}
    assert result["runner_minute_change"] == 3


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
