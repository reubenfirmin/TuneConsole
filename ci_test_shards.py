"""Duration-balanced, whole-file CI shards; local pytest remains serial and unsharded."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import time

import pytest

BASELINE = Path(__file__).parent / ".github/test-durations.json"


def partition(nodeids, durations, count=2):
    """Keep module fixtures together and greedily balance measured file duration."""
    if count < 1 or len(nodeids) != len(set(nodeids)):
        raise ValueError("Shard count must be positive and collected case IDs must be unique")
    files = defaultdict(list)
    for nodeid in nodeids:
        files[nodeid.split("::", 1)[0]].append(nodeid)
    observed = [float(value["seconds"]) / value["cases"] for value in durations.values()
                if value["cases"] > 0 and value["seconds"] > 0]
    unknown_case = statistics.median(observed) if observed else 1.0
    estimates = {}
    for name, cases in files.items():
        measured = durations.get(name)
        estimates[name] = (float(measured["seconds"]) * len(cases) / measured["cases"]
                           if measured and measured["cases"] else unknown_case * len(cases))
        if not math.isfinite(estimates[name]) or estimates[name] < 0:
            raise ValueError(f"Invalid duration estimate for {name}")
    owners, loads = {}, [0.0] * count
    for name in sorted(files, key=lambda name: (-estimates[name], name)):
        owner = min(range(count), key=lambda index: (loads[index], index))
        owners[name] = owner + 1
        loads[owner] += estimates[name]
    return owners, loads


def pytest_addoption(parser):
    group = parser.getgroup("isolated CI shards")
    group.addoption("--ci-shard", metavar="INDEX/TOTAL", help="Serial whole-file shard on this isolated CI runner")
    group.addoption("--ci-suite", choices=("unit", "browser"), default="unit")
    group.addoption("--ci-shard-report", type=Path, help="Record coverage and setup/call/teardown timings as JSON")
    group.addoption("--ci-duration-baseline", type=Path, default=BASELINE)


def pytest_configure(config):
    shard = config.getoption("ci_shard")
    if not shard:
        return
    try:
        index, total = map(int, shard.split("/"))
        if total not in (1, 2) or not 1 <= index <= total:
            raise ValueError()
    except ValueError:
        raise pytest.UsageError("--ci-shard must be 1/2 or 2/2 (1/1 is reserved for hosted baseline measurements)") from None
    path = config.getoption("ci_shard_report")
    if path is None:
        raise pytest.UsageError("--ci-shard requires --ci-shard-report so coverage can be verified")
    baseline_path = config.getoption("ci_duration_baseline")
    suite = config.getoption("ci_suite")
    if baseline_path.is_dir():
        measured = json.loads((baseline_path / f"{suite}-timings.json").read_text())
        if measured.get("suite") != suite or not measured.get("files"):
            raise pytest.UsageError("Shard timing artifact is missing this suite's measured file durations")
        durations = measured["files"]
    else:
        durations = json.loads(baseline_path.read_text()).get(suite, {})
    config.pluginmanager.register(Shard(config, index, total, path, suite, durations), "isolated-ci-shard")


class Shard:
    def __init__(self, config, index, total, path, suite, durations):
        self.config, self.path, self.durations = config, path, durations
        self.started = time.monotonic()
        self.report = {"schema": 2, "suite": suite, "index": index, "total": total,
                       "revision": os.environ.get("GITHUB_SHA", "local"),
                       "started_at": datetime.now(timezone.utc).isoformat(), "tests": {},
                       "executions": [], "repeated_phases": []}

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(self, config, items):
        # Run after normal -m/-k deselection and browser auto-marking, so expected
        # includes every intended parametrized case from normal collection.
        yield
        nodeids = [item.nodeid for item in items]
        owners, loads = partition(nodeids, self.durations, self.report["total"])
        selected, deselected = [], []
        for item in items:
            (selected if owners[item.nodeid.split("::", 1)[0]] == self.report["index"] else deselected).append(item)
        self.report.update(expected=nodeids, selected=[item.nodeid for item in selected],
                           file_owners=owners, estimated_seconds=loads)
        items[:] = selected
        config.hook.pytest_deselected(items=deselected)
        self.write()

    def pytest_runtest_logstart(self, nodeid, location):
        self.report["executions"].append(nodeid)

    def pytest_runtest_logreport(self, report):
        phases = self.report["tests"].setdefault(report.nodeid, {})
        if report.when in phases:
            self.report["repeated_phases"].append([report.nodeid, report.when])
        phases[report.when] = {"seconds": report.duration, "outcome": report.outcome}

    def pytest_sessionfinish(self, session, exitstatus):
        self.report.update(exitstatus=int(exitstatus), elapsed_seconds=time.monotonic() - self.started)
        self.write()

    def write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.report, indent=2) + "\n")


def verify(reports, suite, total=2):
    reports = sorted(reports, key=lambda report: report["index"])
    if total not in (1, 2) or len(reports) != total or [report["index"] for report in reports] != list(range(1, total + 1)):
        raise ValueError(f"{suite}: all {total} isolated shard reports are required")
    original = reports[0]["expected"]
    if not original or len(original) != len(set(original)):
        raise ValueError(f"{suite}: normal collection must be nonempty and unique")
    executed = []
    for report in reports:
        if (report.get("schema"), report["suite"], report["total"], report["revision"], report["expected"], report["file_owners"]) != (
                2, suite, total, reports[0]["revision"], original, reports[0]["file_owners"]):
            raise ValueError(f"{suite}: shard normal collections, file plans or source revisions differ")
        if report.get("exitstatus") != 0:
            raise ValueError(f"{suite}: shard {report['index']} did not pass")
        planned = [nodeid for nodeid in original if report["file_owners"][nodeid.split("::", 1)[0]] == report["index"]]
        if report["selected"] != planned or set(report["tests"]) != set(planned):
            raise ValueError(f"{suite}: missing or unexpected executed cases on shard {report['index']}")
        if Counter(report["executions"]) != Counter(planned) or report["repeated_phases"]:
            raise ValueError(f"{suite}: cases and their phases must execute exactly once, without retries")
        for phases in report["tests"].values():
            if (not {"setup", "teardown"} <= phases.keys()
                    or ("call" not in phases and phases["setup"]["outcome"] != "skipped")):
                raise ValueError(f"{suite}: interrupted test did not complete its phases")
            if (not phases.keys() <= {"setup", "call", "teardown"}
                    or any(phase["outcome"] not in {"passed", "skipped"}
                           or not math.isfinite(phase["seconds"]) or phase["seconds"] < 0 for phase in phases.values())):
                raise ValueError(f"{suite}: failed phase or invalid timing in shard report")
        executed.extend(report["executions"])
    if Counter(executed) != Counter(original):
        raise ValueError(f"{suite}: shards must execute every collected case exactly once")
    return {"suite": suite, "revision": reports[0]["revision"], "shards": total, "cases": len(original),
            "case_digest": hashlib.sha256(json.dumps(sorted(original)).encode()).hexdigest(),
            "estimated_seconds": reports[0]["estimated_seconds"],
            "shard_seconds": [report["elapsed_seconds"] for report in reports],
            "test_runner_minutes": sum(report["elapsed_seconds"] for report in reports) / 60,
            "phase_seconds": {name: sum(phases.get(name, {}).get("seconds", 0)
                                         for report in reports for phases in report["tests"].values())
                              for name in ("setup", "call", "teardown")},
            "skipped": [nodeid for report in reports for nodeid, phases in report["tests"].items()
                        if any(phase["outcome"] == "skipped" for phase in phases.values())],
            "files": {name: {"seconds": sum(phase["seconds"] for report in reports
                                             for nodeid, phases in report["tests"].items()
                                             if nodeid.split("::", 1)[0] == name for phase in phases.values()),
                              "cases": sum(nodeid.split("::", 1)[0] == name for nodeid in original)}
                      for name in reports[0]["file_owners"]}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("suite", choices=("unit", "browser"))
    parser.add_argument("--total", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    reports = [json.loads(path.read_text()) for path in args.directory.rglob("shard.json")]
    result = verify([report for report in reports if report["suite"] == args.suite], args.suite, args.total)
    print(json.dumps(result, indent=2))
    output = args.directory / f"{args.suite}-timings.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
