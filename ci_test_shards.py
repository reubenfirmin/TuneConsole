"""Duration-balanced, whole-file CI shards, each using one serial pytest process."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from json import dumps as _journal_dumps
import math
import os
from os import fsync as _journal_fsync
from pathlib import Path
import platform
import statistics
import time
import uuid

import pytest

BASELINE = Path(__file__).parent / ".github/test-durations.json"
SHARD_COUNTS = (1, 2, 4, 8)
DIAGNOSTIC_BYTES = 64 * 1024
BROWSER_BUDGET_SECONDS = 20 * 60


def _diagnostic(value):
    raw = str(value).encode('utf-8', errors='replace')
    result = raw.decode('utf-8', errors='replace')
    width = DIAGNOSTIC_BYTES // 2
    while len(_journal_dumps(result, ensure_ascii=False).encode()) > DIAGNOSTIC_BYTES:
        result = (raw[:width] + b'\n[diagnostic truncated]\n' + raw[-width:]).decode('utf-8', errors='replace')
        width //= 2
    return result


def _replay(report, event):
    kind = event['event']
    node = event.get('nodeid')
    if kind == 'start':
        report['executions'].append(node)
        report['active'] = {'nodeid': node, 'phase': 'starting'}
    elif kind == 'phase':
        report['active'] = {'nodeid': node, 'phase': event['phase']}
    elif kind == 'outcome':
        phases = report['tests'].setdefault(node, {})
        if event['phase'] in phases:
            report['repeated_phases'].append([node, event['phase']])
        phases[event['phase']] = event['result']
    elif kind == 'end':
        report['active'] = None
    elif kind == 'finish':
        report['exitstatus'] = event['exitstatus']
        if 'elapsed_seconds' in event:  # Previously completed journals remain readable.
            report['elapsed_seconds'] = event['elapsed_seconds']
    elif kind == 'timing':
        if 'exitstatus' not in report:
            raise ValueError('Shard timing precedes session completion')
        report.update(exitstatus=event['exitstatus'], elapsed_seconds=event['elapsed_seconds'])
        if 'process_cpu_seconds' in event:
            report['process_cpu_seconds'] = event['process_cpu_seconds']
    else:
        raise ValueError('Unknown shard journal event')


def load_report(path):
    """Recover complete journal records even when pytest never reached sessionfinish."""
    path = Path(path)
    report = json.loads(path.read_text())
    if 'journal_id' not in report:
        return report  # Previously completed artifacts remain readable.
    report.update(tests={}, executions=[], repeated_phases=[], active=None)
    report.pop('exitstatus', None)
    report.pop('elapsed_seconds', None)
    report.pop('process_cpu_seconds', None)
    journal = path.with_suffix('.events.jsonl')
    with journal.open('rb') as stream:
        sequence = 0
        while raw := stream.readline(4 * DIAGNOSTIC_BYTES + 1):
            if len(raw) > 4 * DIAGNOSTIC_BYTES:
                raise ValueError('Oversized shard journal record')
            if not raw.endswith(b'\n'):
                # A killed writer may leave only its final append incomplete.
                report['journal_incomplete'] = True
                report.pop('exitstatus', None)
                break
            event = json.loads(raw)
            if event.get('journal_id') != report['journal_id'] or event.get('sequence') != sequence:
                raise ValueError('Shard journal source or sequence differs')
            _replay(report, event)
            sequence += 1
    if 'exitstatus' in report and 'elapsed_seconds' not in report:
        # Session completion alone excludes terminal synchronization and cleanup.
        # A killed process without the final timing receipt cannot certify a run.
        report['journal_incomplete'] = True
    if report.get('journal_incomplete'):
        report.pop('exitstatus', None)
    return report


def calibrate_unit_timings(current, previous):
    """Compare crossed source cohorts before using one runner's time as a file cost."""
    current_sources = {row.get('source_shard') for row in current.values()}
    previous_sources = {row.get('source_shard') for row in previous.values()}
    if (min(len(current_sources), len(previous_sources)) < 2
            or any(type(source) is not int or source < 1 for source in current_sources | previous_sources)):
        return current, {}
    ratios = defaultdict(list)
    for name, row in current.items():
        before = previous.get(name)
        if before is None or row['cases'] != before['cases']:
            continue
        earlier, later = before['seconds'], row['seconds']
        if not all(math.isfinite(value) and value >= .2 for value in (earlier, later)):
            continue  # Very short files mostly measure timer and fixture noise.
        ratios[before['source_shard'], row['source_shard']].append(math.log(later) - math.log(earlier))
    cells = {pair: statistics.median(values) for pair, values in ratios.items() if len(values) >= 4}
    residuals = defaultdict(list)
    for source in sorted(previous_sources):
        row = {new: ratio for (old, new), ratio in cells.items() if old == source}
        if len(row) < max(2, (len(current_sources) + 1) // 2):
            return current, {}  # No reliable same-source comparison for this prior runner.
        middle = statistics.median(row.values())
        for new, ratio in row.items():
            residuals[new].append(ratio - middle)
    if any(len(residuals[new]) < max(2, (len(previous_sources) + 1) // 2) for new in current_sources):
        return current, {}
    offsets = {new: statistics.median(residuals[new]) for new in sorted(current_sources)}
    center = statistics.median(offsets.values())
    if any(abs(offset - center) > math.log(2) for offset in offsets.values()):
        return current, {}  # Incompatible measurements are not a timing calibration.
    scales = {source: math.exp(offset - center) for source, offset in offsets.items()}
    return {name: {**row, 'seconds': row['seconds'] / scales[row['source_shard']]}
            for name, row in current.items()}, scales


def partition(nodeids, durations, count=2, *, capacities=None):
    """Keep files together and spread each timing source across the new shards."""
    if count < 1 or len(nodeids) != len(set(nodeids)):
        raise ValueError("Shard count must be positive and collected case IDs must be unique")
    capacities = [1.0] * count if capacities is None else list(capacities)
    if len(capacities) != count or any(type(value) not in (int, float)
                                      or not math.isfinite(value) or value <= 0 for value in capacities):
        raise ValueError('Every shard needs a positive finite runner capacity')
    files = defaultdict(list)
    for nodeid in nodeids:
        files[nodeid.split("::", 1)[0]].append(nodeid)
    observed = [float(value["seconds"]) / value["cases"] for value in durations.values()
                if value["cases"] > 0 and value["seconds"] > 0]
    unknown_case = statistics.median(observed) if observed else 1.0
    estimates, sources = {}, defaultdict(list)
    for name, cases in files.items():
        measured = durations.get(name)
        # Removing consumers does not remove a module's fixture construction
        # or retained workflow. Keep its measured whole-file cost until a clean
        # run replaces it; still account for growth beyond the measured cases.
        estimates[name] = (float(measured["seconds"]) * max(1, len(cases) / measured["cases"])
                           if measured and measured["cases"] else unknown_case * len(cases))
        if not math.isfinite(estimates[name]) or estimates[name] < 0:
            raise ValueError(f"Invalid duration estimate for {name}")
        source = measured.get('source_shard', 0) if measured else 0
        if type(source) is not int or source < 0:
            raise ValueError(f"Invalid source shard for {name}")
        sources[source].append(name)
    owners, loads = {}, [0.0] * count
    # Errors in one earlier runner's absolute times can affect many files
    # together. Spread that source's work instead of concentrating its error.
    for source in sorted(sources, key=lambda source: (-sum(estimates[name] for name in sources[source]), source)):
        source_loads = [0.0] * count
        for name in sorted(sources[source], key=lambda name: (-estimates[name], name)):
            owner = min(range(count), key=lambda index: (
                source_loads[index] / capacities[index],
                (loads[index] + estimates[name]) / capacities[index], index))
            owners[name] = owner + 1
            loads[owner] += estimates[name]
            source_loads[owner] += estimates[name]
    return owners, loads


def pytest_addoption(parser):
    group = parser.getgroup("isolated CI shards")
    group.addoption("--ci-shard", metavar="INDEX/TOTAL", help="Serial whole-file shard on this isolated CI runner")
    group.addoption("--ci-suite", choices=("unit", "browser"), default="unit")
    group.addoption("--ci-shard-report", type=Path, help="Record coverage and setup/call/teardown timings as JSON")
    group.addoption("--ci-source-report", type=Path, help="Record Python source hashes before and after the measured run")
    group.addoption("--ci-duration-baseline", type=Path, default=BASELINE)
    group.addoption('--ci-runner-capacities', type=Path, help='Current-attempt unit runner capacity manifests')


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_cmdline_main(config):
    started = time.monotonic()
    cpu_started = time.process_time()
    exitstatus = yield
    shard = config.pluginmanager.get_plugin('isolated-ci-shard')
    if shard is not None and not shard.journal_failed and 'exitstatus' in shard.report:
        # The command has returned through sessionfinish and unconfigure,
        # including journal synchronization, reports and registered cleanup.
        # Only publication of this small timing receipt follows the clock read.
        shard.event('timing', exitstatus=int(exitstatus), elapsed_seconds=time.monotonic() - started,
                    process_cpu_seconds=time.process_time() - cpu_started)
    return exitstatus


def pytest_configure(config):
    shard = config.getoption("ci_shard")
    if not shard:
        return
    try:
        index, total = map(int, shard.split("/"))
        if total not in SHARD_COUNTS or not 1 <= index <= total:
            raise ValueError()
    except ValueError:
        raise pytest.UsageError("--ci-shard must be INDEX/TOTAL with 1, 2, 4 or 8 serial runners and 1 <= INDEX <= TOTAL") from None
    path = config.getoption("ci_shard_report")
    if path is None:
        raise pytest.UsageError("--ci-shard requires --ci-shard-report so coverage can be verified")
    baseline_path = config.getoption("ci_duration_baseline")
    suite = config.getoption("ci_suite")
    source_scales = {}
    if baseline_path.is_dir():
        measured = json.loads((baseline_path / f"{suite}-timings.json").read_text())
        if measured.get("suite") != suite or not measured.get("files"):
            raise pytest.UsageError("Shard timing artifact is missing this suite's measured file durations")
        durations = measured["files"]
        if suite == 'unit':
            reference = json.loads(BASELINE.read_text())
            if reference.get('failed_cases') == [] and measured.get('revision') != reference.get('revision'):
                durations, source_scales = calibrate_unit_timings(durations, reference.get('unit', {}))
    else:
        durations = json.loads(baseline_path.read_text()).get(suite, {})
    capacity_path = config.getoption('ci_runner_capacities')
    capacities, probes = None, None
    if capacity_path is not None:
        if suite != 'unit':
            raise pytest.UsageError('Current runner capacities apply only to unit shards')
        from ci_runner_capacity import load_capacities
        capacities, probes = load_capacities(capacity_path, total)
    config.pluginmanager.register(Shard(config, index, total, path, suite, durations, source_scales=source_scales,
                                      capacities=capacities, capacity_probes=probes,
                                      source_path=config.getoption('ci_source_report')), "isolated-ci-shard")


def _cpu_model():
    if platform.system() == 'Linux':
        try:
            with open('/proc/cpuinfo') as info:
                for line in info.read(8192).splitlines():
                    key, _, value = line.partition(':')
                    if key.strip() == 'model name':
                        return value.strip()[:128]
        except OSError:
            pass
    return platform.processor()


class Shard:
    def __init__(self, config, index, total, path, suite, durations, *, source_path=None, source_scales=None,
                 capacities=None, capacity_probes=None):
        self.config, self.path, self.durations = config, path.resolve(), durations
        self.capacities = capacities
        self.source_path = source_path.resolve() if source_path is not None else None
        if self.source_path is not None:
            self.source_root = Path(config.rootpath).resolve()
            self.source_report = {'arguments': list(config.invocation_params.args),
                                  'sources': self.source_hashes()}
            self.write_sources()
        self.sequence = 0
        self.journal_failed = False
        self._sync = _journal_fsync
        self.journal = self.path.with_suffix('.events.jsonl')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.journal.write_bytes(b'')
        self.report = {"schema": 2, "suite": suite, "index": index, "total": total,
                       "revision": os.environ.get("GITHUB_SHA", "local"),
                       'runtime': {'python': platform.python_version(), 'platform': platform.platform(),
                                   'processor': platform.processor(), 'cpu_count': os.cpu_count(),
                                   'cpu_model': _cpu_model(), 'image_version': os.environ.get('ImageVersion')},
                       "started_at": datetime.now(timezone.utc).isoformat(), "tests": {},
                       "executions": [], "repeated_phases": [], 'journal_id': uuid.uuid4().hex,
                       'active': None}
        if source_scales:
            self.report['planning_source_scales'] = {str(source): scale for source, scale in source_scales.items()}
        if capacities is not None:
            self.report.update(runner_capacities=capacities, capacity_probes=capacity_probes)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(self, config, items):
        # Run after normal -m/-k deselection and browser auto-marking, so expected
        # includes every intended parametrized case from normal collection.
        yield
        nodeids = [item.nodeid for item in items]
        owners, loads = partition(nodeids, self.durations, self.report["total"], capacities=self.capacities)
        selected, deselected = [], []
        for item in items:
            (selected if owners[item.nodeid.split("::", 1)[0]] == self.report["index"] else deselected).append(item)
        self.report.update(expected=nodeids, selected=[item.nodeid for item in selected],
                           file_owners=owners, estimated_seconds=loads)
        if self.capacities is not None:
            self.report['estimated_runner_seconds'] = [load / capacity for load, capacity in zip(loads, self.capacities)]
        items[:] = selected
        config.hook.pytest_deselected(items=deselected)
        self.write()

    def pytest_runtest_logstart(self, nodeid, location):
        self.event('start', nodeid=nodeid)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_setup(self, item):
        self.event('phase', nodeid=item.nodeid, phase='setup')
        yield

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_call(self, item):
        self.event('phase', nodeid=item.nodeid, phase='call')
        yield

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_teardown(self, item):
        self.event('phase', nodeid=item.nodeid, phase='teardown')
        yield

    def pytest_runtest_logreport(self, report):
        result = {"seconds": report.duration, "outcome": report.outcome}
        if report.failed:
            result['diagnostic'] = _diagnostic(report.longreprtext)
            result['captured'] = _diagnostic('\n'.join(f'{name}\n{text}' for name, text in report.sections))
        self.event('outcome', nodeid=report.nodeid, phase=report.when, result=result)

    def pytest_runtest_logfinish(self, nodeid, location):
        self.event('end', nodeid=nodeid)

    def pytest_sessionfinish(self, session, exitstatus):
        if self.journal_failed:
            # The original recording error propagates to pytest. Never append
            # a terminal success or reuse a possibly written sequence number.
            self.report['journal_incomplete'] = True
            self.write()
            return
        self.event('finish', exitstatus=int(exitstatus))
        self.write()
        if self.source_path is not None:
            self.source_report['after_sources'] = self.source_hashes()
            self.write_sources()

    def source_hashes(self):
        paths = [*self.source_root.glob('*.py'),
                 *(self.source_root / 'src').rglob('*.py'),
                 *(self.source_root / 'tests').rglob('*.py')]
        return {str(path.relative_to(self.source_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(paths)}

    def write_sources(self):
        self.source_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.source_path.with_suffix('.json.tmp')
        temporary.write_text(_journal_dumps(self.source_report, indent=2) + '\n')
        temporary.replace(self.source_path)

    def event(self, kind, **values):
        if self.journal_failed:
            raise OSError('Shard journal is unavailable after a recording failure')
        event = {'event': kind, 'journal_id': self.report['journal_id'], 'sequence': self.sequence, **values}
        # Flush every transition so a killed pytest process leaves recoverable
        # receipts. Only synchronize the completed journal: per-event fsync
        # turns runner disk latency into work in every measured test phase.
        try:
            with self.journal.open('ab') as stream:
                # Own these callables independently of product fault injection
                # on the shared json/os modules. Real journal failures still
                # propagate and leave the run explicitly incomplete.
                stream.write((_journal_dumps(event, ensure_ascii=False) + '\n').encode())
                stream.flush()
                if kind == 'finish':
                    self._sync(stream.fileno())
        except BaseException:
            self.journal_failed = True
            self.report['journal_incomplete'] = True
            self.write()
            raise
        self.sequence += 1
        _replay(self.report, event)

    def write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.json.tmp')
        temporary.write_text(_journal_dumps(self.report, indent=2) + "\n")
        temporary.replace(self.path)


def _measure(reports, suite, total, *, diagnostic=False):
    reports = sorted(reports, key=lambda report: report["index"])
    if total not in SHARD_COUNTS or len(reports) != total or [report["index"] for report in reports] != list(range(1, total + 1)):
        raise ValueError(f"{suite}: all {total} isolated shard reports are required")
    original = reports[0]["expected"]
    if not original or len(original) != len(set(original)):
        raise ValueError(f"{suite}: normal collection must be nonempty and unique")
    capacities = reports[0].get('runner_capacities')
    if capacities is not None:
        if (suite != 'unit' or len(capacities) != total
                or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                       for value in capacities)
                or len(reports[0]['estimated_seconds']) != total
                or reports[0].get('estimated_runner_seconds') != [
                    load / capacity for load, capacity in zip(reports[0]['estimated_seconds'], capacities)]):
            raise ValueError(f'{suite}: invalid current runner capacity plan')
    executed = []
    for report in reports:
        if (report.get("schema"), report["suite"], report["total"], report["revision"], report["expected"], report["file_owners"]) != (
                2, suite, total, reports[0]["revision"], original, reports[0]["file_owners"]):
            raise ValueError(f"{suite}: shard normal collections, file plans or source revisions differ")
        if report.get('planning_source_scales') != reports[0].get('planning_source_scales'):
            raise ValueError(f'{suite}: shard timing calibrations differ')
        if any(report.get(key) != reports[0].get(key) for key in (
                'runner_capacities', 'capacity_probes', 'estimated_runner_seconds')):
            raise ValueError(f'{suite}: current runner capacities differ')
        if report.get("exitstatus") not in ((0, 1) if diagnostic else (0,)):
            raise ValueError(f"{suite}: shard {report['index']} did not pass")
        if report.get('active') or report.get('journal_incomplete'):
            raise ValueError(f"{suite}: shard {report['index']} has incomplete journal work")
        elapsed = report.get("elapsed_seconds")
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError(f"{suite}: shard {report['index']} has invalid elapsed time")
        planned = [nodeid for nodeid in original if report["file_owners"][nodeid.split("::", 1)[0]] == report["index"]]
        if report["selected"] != planned or set(report["tests"]) != set(planned):
            raise ValueError(f"{suite}: missing or unexpected executed cases on shard {report['index']}")
        if Counter(report["executions"]) != Counter(planned) or report["repeated_phases"]:
            raise ValueError(f"{suite}: cases and their phases must execute exactly once, without retries")
        for phases in report["tests"].values():
            if (not {"setup", "teardown"} <= phases.keys()
                    or ("call" not in phases and phases["setup"]["outcome"] not in
                        ({"skipped", "failed"} if diagnostic else {"skipped"}))):
                raise ValueError(f"{suite}: interrupted test did not complete its phases")
            if (not phases.keys() <= {"setup", "call", "teardown"}
                    or any(phase["outcome"] not in ({"passed", "skipped", "failed"} if diagnostic else {"passed", "skipped"})
                           or not math.isfinite(phase["seconds"]) or phase["seconds"] < 0 for phase in phases.values())):
                raise ValueError(f"{suite}: failed phase or invalid timing in shard report")
        executed.extend(report["executions"])
    if Counter(executed) != Counter(original):
        raise ValueError(f"{suite}: shards must execute every collected case exactly once")
    elapsed_seconds = sum(report["elapsed_seconds"] for report in reports)
    if not diagnostic and suite == "browser" and elapsed_seconds > BROWSER_BUDGET_SECONDS:
        raise ValueError(f"browser: aggregate pytest elapsed time {elapsed_seconds:.3f}s "
                         f"exceeds the {BROWSER_BUDGET_SECONDS}s total budget")
    return {"suite": suite, "revision": reports[0]["revision"], "shards": total, "cases": len(original),
            "case_digest": hashlib.sha256(json.dumps(sorted(original)).encode()).hexdigest(),
            "estimated_seconds": reports[0]["estimated_seconds"],
            **({'planning_source_scales': reports[0]['planning_source_scales']}
               if 'planning_source_scales' in reports[0] else {}),
            **({key: reports[0][key] for key in ('runner_capacities', 'capacity_probes', 'estimated_runner_seconds')}
               if 'runner_capacities' in reports[0] else {}),
            "shard_seconds": [report["elapsed_seconds"] for report in reports],
            "test_runner_minutes": elapsed_seconds / 60,
            "phase_seconds": {name: sum(phases.get(name, {}).get("seconds", 0)
                                         for report in reports for phases in report["tests"].values())
                              for name in ("setup", "call", "teardown")},
            "skipped": [nodeid for report in reports for nodeid, phases in report["tests"].items()
                        if any(phase["outcome"] == "skipped" for phase in phases.values())],
            "files": {name: {"seconds": sum(phase["seconds"] for report in reports
                                             for nodeid, phases in report["tests"].items()
                                             if nodeid.split("::", 1)[0] == name for phase in phases.values()),
                              "cases": sum(nodeid.split("::", 1)[0] == name for nodeid in original),
                              **({'source_shard': reports[0]['file_owners'][name]} if suite == 'unit' else {})}
                      for name in reports[0]["file_owners"]}}


def verify(reports, suite, total=2):
    """Accept passing, exactly-once coverage and enforce the browser budget."""
    return _measure(reports, suite, total)


def refresh_baseline(reports, run, total=4, *, browser_total=None):
    """Use complete diagnostic receipts as estimates, never passing coverage.

    This explicit maintenance path can consume a failed run. The normal CI
    verifier and successful-workflow artifact selection remain unchanged.
    """
    if {report['suite'] for report in reports} != {'unit', 'browser'}:
        raise ValueError('Baseline refresh requires both unit and browser receipts')
    counts = {'unit': total, 'browser': total if browser_total is None else browser_total}
    measured = {suite: _measure([row for row in reports if row['suite'] == suite],
                                suite, counts[suite], diagnostic=True)
                for suite in ('unit', 'browser')}
    if measured['unit']['revision'] != measured['browser']['revision']:
        raise ValueError('Baseline suites must use the same source revision')
    collections = {suite: set(next(row['expected'] for row in reports if row['suite'] == suite))
                   for suite in ('unit', 'browser')}
    if collections['unit'] & collections['browser']:
        raise ValueError('Baseline suites must have disjoint case collections')
    return {
        'schema': 1,
        'source': 'Complete, consistent, exactly-once CI setup/call/teardown receipts. '
                  'Diagnostic partition estimates only; failures are retained below. '
                  'This is not passing coverage, budget acceptance or a measured speedup.',
        'run': run,
        'revision': measured['unit']['revision'],
        'refreshed_at': datetime.now(timezone.utc).isoformat(),
        'failed_cases': sorted(node for row in reports for node, phases in row['tests'].items()
                               if any(phase['outcome'] == 'failed' for phase in phases.values())),
        'measurements': {suite: {key: result[key] for key in
                                ('cases', 'case_digest', 'shards', 'shard_seconds', 'phase_seconds')}
                         for suite, result in measured.items()},
        **{suite: result['files'] for suite, result in measured.items()},
    }


def baseline_json(result):
    # Keep measured files to one line each so weight changes remain reviewable.
    text = json.dumps({key: value for key, value in result.items()
                       if key not in ('unit', 'browser')}, indent=2)[:-2]
    for suite in ('unit', 'browser'):
        rows = ',\n'.join('    ' + json.dumps(name) + ': ' + json.dumps(row, separators=(',', ':'))
                          for name, row in sorted(result[suite].items()))
        text += ',\n  ' + json.dumps(suite) + ': {\n' + rows + '\n  }'
    return text + '\n}\n'


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("suite", choices=("unit", "browser", "baseline"))
    parser.add_argument("--total", type=int, choices=SHARD_COUNTS, default=4)
    parser.add_argument("--browser-total", type=int, choices=SHARD_COUNTS,
                        help="Browser shard count when refreshing a baseline with different suite sizes")
    parser.add_argument("--run", help="Source CI run URL (required for an explicit baseline refresh)")
    parser.add_argument("--output", type=Path, help="Destination for the explicit diagnostic baseline")
    args = parser.parse_args()
    if (args.suite == 'baseline') != bool(args.run and args.output):
        parser.error('baseline requires --run and --output; coverage verification accepts neither')
    if args.suite != 'baseline' and (args.run or args.output):
        parser.error('--run and --output are only for baseline refresh')
    if args.suite != 'baseline' and args.browser_total is not None:
        parser.error('--browser-total is only for baseline refresh')
    reports = [load_report(path) for path in args.directory.rglob("shard.json")]
    if args.suite == 'baseline':
        result = refresh_baseline(reports, args.run, args.total, browser_total=args.browser_total)
        args.output.write_text(baseline_json(result))
        print(json.dumps({suite: {'cases': result['measurements'][suite]['cases'],
                                  'files': len(result[suite]),
                                  'estimated_seconds': partition(
                                      next(row['expected'] for row in reports if row['suite'] == suite),
                                      result[suite], result['measurements'][suite]['shards'])[1]}
                          for suite in ('unit', 'browser')}, indent=2))
        raise SystemExit(0)
    result = verify([report for report in reports if report["suite"] == args.suite], args.suite, args.total)
    print(json.dumps(result, indent=2))
    output = args.directory / f"{args.suite}-timings.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
