"""Exact parametrized-case coverage and measurements across isolated serial runners."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from ci_test_shards import (BASELINE, DIAGNOSTIC_BYTES, _diagnostic, calibrate_unit_timings, load_report, partition,
                            pytest_configure as configure_shard, refresh_baseline, verify)

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


def test_removed_consumers_do_not_erase_measured_module_setup_from_the_plan():
    # A rewrite can retain a costly shared workflow with fewer assertions.
    # Its clean whole-file measurement remains the available cost evidence.
    nodes = ['retained.py::test_original', 'other.py::test_a', 'other.py::test_b']
    times = {'retained.py': {'seconds': 100, 'cases': 50},
             'other.py': {'seconds': 60, 'cases': 2}}
    owners, loads = partition(nodes, times, 2)
    assert owners == {'retained.py': 1, 'other.py': 2}
    assert loads == [100, 60]
    assert owners == partition(list(reversed(nodes)), times, 2)[0]
    expanded = [f'retained.py::test_case[{i}]' for i in range(100)] + nodes[1:]
    expanded_owners, expanded_loads = partition(expanded, times, 2)
    assert expanded_owners == owners
    assert expanded_loads == [200, 60]


@pytest.mark.parametrize('total', [2, 4, 8])
def test_source_balance_spreads_correlated_estimation_errors(total):
    # Equal absolute estimates can put every underestimated source-A file on
    # one set of runners and every source-B file on another. Use the source
    # identities to distribute both kinds of work without changing their costs.
    timings = {f'{source}-{weight}.py': {'seconds': weight, 'cases': 2, 'source_shard': index}
               for index, source in enumerate(('a', 'b'), 1) for weight in range(1, 2 * total + 1)}
    nodes = [f'{name}::test_case[{case}]' for name in timings for case in range(2)]
    observed = {name: row['seconds'] * (2 if name.startswith('a-') else 1)
                for name, row in timings.items()}
    legacy = {name: {key: value for key, value in row.items() if key != 'source_shard'}
              for name, row in timings.items()}
    legacy_owners, legacy_loads = partition(nodes, legacy, total)
    legacy_actual = [sum(observed[name] for name, owner in legacy_owners.items() if owner == index)
                     for index in range(1, total + 1)]
    assert max(legacy_loads) == min(legacy_loads)
    assert max(legacy_actual) == 2 * min(legacy_actual)

    owners, loads = partition(nodes, timings, total)
    assert owners == partition(list(reversed(nodes)), dict(reversed(list(timings.items()))), total)[0]
    assert set(owners) == set(timings)
    assert sum(loads) == sum(row['seconds'] for row in timings.values())
    actual = [sum(observed[name] for name, owner in owners.items() if owner == index)
              for index in range(1, total + 1)]
    assert max(loads) == min(loads)
    assert max(actual) == min(actual)


@pytest.mark.parametrize('source', [None, '1', -1, True])
def test_invalid_source_identity_is_rejected(source):
    with pytest.raises(ValueError, match='Invalid source shard'):
        partition(['one.py::test_case'], {'one.py': {'seconds': 1, 'cases': 1, 'source_shard': source}})


def test_source_balance_keeps_new_files_and_legacy_measurements(tmp_path, monkeypatch):
    timings = {'a.py': {'seconds': 8, 'cases': 2, 'source_shard': 1},
               'b.py': {'seconds': 4, 'cases': 2, 'source_shard': 2},
               'legacy.py': {'seconds': 2, 'cases': 1}}
    nodes = ['a.py::test_one', 'a.py::test_two', 'b.py::test_one', 'b.py::test_two',
             'legacy.py::test_one', 'new.py::test_one', 'new.py::test_two']
    owners, loads = partition(nodes, timings, 4)
    assert set(owners) == {'a.py', 'b.py', 'legacy.py', 'new.py'}
    assert sum(loads) == 18  # The two new cases retain the measured 2s median estimate.
    assert owners == partition(list(reversed(nodes)), timings, 4)[0]

    # The same files crossed differently paced runners in two clean passes.
    # Shared fixture drift in one file must not become a runner-wide weight.
    earlier, measured, costs = {}, {}, {}
    for old in (1, 2):
        for new in (1, 2):
            for i in range(5):
                name = f'{old}-{new}-{i}.py'
                cost = i + 1
                earlier[name] = {'seconds': cost * (1.5 if old == 1 else 1), 'cases': 2, 'source_shard': old}
                drift = 4 if (old, new, i) == (1, 2, 4) else 1
                costs[name] = cost * drift
                measured[name] = {'seconds': costs[name] * new, 'cases': 2, 'source_shard': new}
    measured['new.py'] = {'seconds': 6, 'cases': 1, 'source_shard': 2}
    earlier['changed.py'] = {'seconds': 100, 'cases': 1, 'source_shard': 1}
    measured['changed.py'] = {'seconds': 4, 'cases': 2, 'source_shard': 1}
    original = deepcopy(measured)
    adjusted, scales = calibrate_unit_timings(measured, earlier)
    assert scales == pytest.approx({1: 2 ** -.5, 2: 2 ** .5})
    assert measured == original
    assert adjusted['new.py']['seconds'] == pytest.approx(3 * 2 ** .5)
    assert adjusted['changed.py']['seconds'] == pytest.approx(4 * 2 ** .5)
    assert all(adjusted[name]['seconds'] == pytest.approx(cost * 2 ** .5) for name, cost in costs.items())
    assert calibrate_unit_timings(measured, {name: row for name, row in earlier.items()
                                            if row['source_shard'] == 1}) == (measured, {})
    committed = tmp_path / 'baseline.json'
    committed.write_text(json.dumps({'revision': 'earlier', 'failed_cases': [], 'unit': earlier}))
    monkeypatch.setattr('ci_test_shards.BASELINE', committed)
    artifact = tmp_path / 'timings'
    artifact.mkdir()
    (artifact / 'unit-timings.json').write_text(json.dumps({'revision': 'current', 'suite': 'unit', 'files': measured}))
    options = {'ci_shard': '1/8', 'ci_shard_report': tmp_path / 'shard.json', 'ci_suite': 'unit',
               'ci_duration_baseline': artifact, 'ci_source_report': None}
    registered = []
    configure_shard(SimpleNamespace(getoption=options.__getitem__, pluginmanager=SimpleNamespace(
        register=lambda plugin, name: registered.append(plugin))))
    assert registered[0].durations == adjusted
    assert registered[0].report['planning_source_scales'] == pytest.approx({str(k): v for k, v in scales.items()})


@pytest.mark.parametrize("suite,total", [("unit", 8), ("browser", 4)])
def test_retained_measured_timings_balance_whole_file_groups(suite, total):
    baseline = json.loads(BASELINE.read_text())
    timings = baseline[suite]
    nodes = [f"{name}::recorded_case[{case}]" for name, row in timings.items() for case in range(row["cases"])]
    owners, loads = partition(nodes, timings, total)
    assert set(owners.values()) == set(range(1, total + 1))
    assert sum(loads) == pytest.approx(sum(row["seconds"] for row in timings.values()))
    assert max(loads) / min(loads) < 1.02
    assert baseline["run"].startswith('https://github.com/reubenfirmin/TuneConsole/actions/runs/')
    assert baseline["failed_cases"] == []  # Committed weights now use a verified passing run.
    assert len(nodes) == baseline['measurements'][suite]['cases']
    if suite == 'unit':
        assert {row['source_shard'] for row in timings.values()} == set(range(1, 9))


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


@pytest.mark.parametrize('total', [4, 8])
def test_shards_cover_every_parameter_once_and_keep_modules_together(total):
    nodeids = [f"file-{file}.py::test_case[{case}]" for file in range(3 * total) for case in range(3)]
    rows = reports(total, nodeids)
    assert verify(rows, "unit", total=total)["case_digest"] == verify(reports(1, nodeids), "unit", total=1)["case_digest"]
    assert all(len(row["selected"]) == 9 for row in rows)
    assert set(rows[0]["file_owners"].values()) == set(range(1, total + 1))
    with pytest.raises(ValueError, match=f"all {total}"):
        verify(rows[:-1], "unit", total=total)
    rows[-1]["exitstatus"] = 1
    with pytest.raises(ValueError, match="did not pass"):
        verify(rows, "unit", total=total)


@pytest.mark.parametrize("shard", ["1/1", "2/2", "1/4", "4/4", "1/8", "8/8", "0/4", "5/4", "1/3", "9/8", "1/16"])
def test_runner_configuration_accepts_supported_splits_and_rejects_invalid_indices(tmp_path, shard):
    options = {"ci_shard": shard, "ci_shard_report": tmp_path / "shard.json",
               "ci_suite": "unit", "ci_duration_baseline": BASELINE, 'ci_source_report': None}
    registered = []
    config = SimpleNamespace(getoption=options.__getitem__, pluginmanager=SimpleNamespace(
        register=lambda plugin, name: registered.append(plugin)))
    if shard in {"0/4", "5/4", "1/3", "9/8", "1/16"}:
        with pytest.raises(pytest.UsageError, match="INDEX/TOTAL"):
            configure_shard(config)
    else:
        configure_shard(config)
        assert registered[0].report["index"] == int(shard.split("/")[0])
        assert registered[0].report["total"] == int(shard.split("/")[1])


def test_every_parameter_case_executes_once_and_all_phases_feed_timings():
    rows = reports()
    for row in rows:
        row['planning_source_scales'] = {'1': .8, '2': 1.2}
    result = verify(rows, "unit")
    assert result["cases"] == 3
    assert result["files"]["one.py"]["cases"] == 2
    assert result["files"]["one.py"]["seconds"] == pytest.approx(.6)
    assert result["test_runner_minutes"] == pytest.approx(20 / 60)
    assert result["phase_seconds"] == pytest.approx({"setup": .3, "call": .3, "teardown": .3})
    assert {name: row['source_shard'] for name, row in result['files'].items()} == rows[0]['file_owners']
    assert result['planning_source_scales'] == {'1': .8, '2': 1.2}
    rows[-1]['planning_source_scales'] = {'1': 1, '2': 1}
    with pytest.raises(ValueError, match='timing calibrations differ'):
        verify(rows, 'unit')
    rows[-1]['planning_source_scales'] = rows[0]['planning_source_scales'].copy()
    for row in rows:
        row.update(runner_capacities=[1, 2], capacity_probes=[{'index': 1}, {'index': 2}],
                   estimated_runner_seconds=[row['estimated_seconds'][0], row['estimated_seconds'][1] / 2])
    result = verify(rows, 'unit')
    assert result['runner_capacities'] == [1, 2]
    assert result['capacity_probes'] == rows[0]['capacity_probes']
    assert result['estimated_runner_seconds'] == [2, .5]
    for key, value in (('runner_capacities', [2, 1]), ('capacity_probes', [{'index': 1}])):
        before = rows[-1][key]
        rows[-1][key] = value
        with pytest.raises(ValueError, match='current runner capacities differ'):
            verify(rows, 'unit')
        rows[-1][key] = before
    rows[0]['runner_capacities'] = [0, 2]
    with pytest.raises(ValueError, match='invalid current runner capacity plan'):
        verify(rows, 'unit')


def test_browser_measurements_keep_the_existing_allocation_input():
    rows = reports()
    for row in rows:
        row['suite'] = 'browser'
    assert all('source_shard' not in measured for measured in verify(rows, 'browser')['files'].values())


def test_baseline_refresh_retains_eight_unit_and_four_browser_receipts():
    rows = reports(8)
    browser = reports(4, ['browser.py::test_case'])
    for row in browser:
        row['suite'] = 'browser'
    result = refresh_baseline(rows + browser, 'https://example.test/run', total=8, browser_total=4)
    assert result['measurements']['unit']['shards'] == 8
    assert result['measurements']['browser']['shards'] == 4
    with pytest.raises(ValueError, match='all 4'):
        refresh_baseline(rows + browser[:-1], 'https://example.test/run', total=8, browser_total=4)


@pytest.mark.parametrize("total", [1, 2, 4])
def test_browser_budget_counts_all_runners_and_is_independent_of_case_count(total):
    rows = reports(total, [f"file-{n}.py::test_case" for n in range(8)])
    for row in rows:
        row.update(suite="browser", elapsed_seconds=1200 / total)
    assert verify(rows, "browser", total)["test_runner_minutes"] == 20
    rows[-1]["elapsed_seconds"] += .001
    with pytest.raises(ValueError, match="aggregate.*1200s total budget"):
        verify(rows, "browser", total)
    for row in rows:
        row["suite"] = "unit"
    assert verify(rows, "unit", total)["test_runner_minutes"] > 20


@pytest.mark.parametrize("elapsed", [None, "300", float("nan"), float("inf"), -1])
def test_invalid_process_timings_cannot_establish_the_budget(elapsed):
    rows = reports()
    rows[0]["elapsed_seconds"] = elapsed
    with pytest.raises(ValueError, match="invalid elapsed time"):
        verify(rows, "unit")


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


def diagnostic_reports():
    rows = reports()
    browser = reports(nodeids=['browser/' + node for node in rows[0]['expected']])
    for row in browser:
        row.update(suite='browser', elapsed_seconds=700)
    return rows + browser


def test_failed_complete_receipts_refresh_estimates_without_accepting_coverage_or_browser_budget():
    rows = diagnostic_reports()
    node = rows[0]['executions'][0]
    rows[0]['tests'][node]['call']['outcome'] = 'failed'
    rows[0]['exitstatus'] = 1
    result = refresh_baseline(rows, 'https://example.test/run', total=2)
    assert result['failed_cases'] == [node]
    assert result['unit']['one.py']['seconds'] == pytest.approx(.6)
    assert result['measurements']['browser']['shard_seconds'] == [700, 700]
    assert result['measurements']['unit']['case_digest'] == verify(reports(), 'unit')['case_digest']
    with pytest.raises(ValueError, match='did not pass'):
        verify(rows[:2], 'unit')
    with pytest.raises(ValueError, match='1200s total budget'):
        verify(rows[2:], 'browser')


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'repeated-phase', 'active', 'journal',
                                  'interrupted', 'missing-call', 'exit', 'revision', 'missing-suite', 'overlap'])
def test_diagnostic_refresh_rejects_incomplete_or_inconsistent_work(fault):
    rows = diagnostic_reports()
    node = rows[0]['executions'][0]
    if fault == 'missing':
        rows[0]['tests'].pop(node)
    elif fault == 'duplicate':
        rows[0]['executions'].append(node)
    elif fault == 'repeated-phase':
        rows[0]['repeated_phases'].append([node, 'call'])
    elif fault == 'active':
        rows[0]['active'] = {'nodeid': node, 'phase': 'call'}
    elif fault == 'journal':
        rows[0]['journal_incomplete'] = True
    elif fault in ('interrupted', 'missing-call'):
        rows[0]['tests'][node].pop('teardown' if fault == 'interrupted' else 'call')
    elif fault == 'exit':
        rows[0]['exitstatus'] = 2
    elif fault == 'revision':
        for row in rows[2:]:
            row['revision'] = 'another source'
    elif fault == 'overlap':
        rows[2:] = deepcopy(rows[:2])
        for row in rows[2:]:
            row['suite'] = 'browser'
    else:
        rows = rows[:2]
    with pytest.raises(ValueError):
        refresh_baseline(rows, 'https://example.test/run', total=2)


def test_completed_setup_failure_retains_its_cost_and_identity_in_diagnostic_estimates():
    rows = diagnostic_reports()
    node = rows[0]['executions'][0]
    phases = rows[0]['tests'][node]
    phases['setup']['outcome'] = 'failed'
    phases.pop('call')
    rows[0]['exitstatus'] = 1
    result = refresh_baseline(rows, 'https://example.test/run', total=2)
    assert result['failed_cases'] == [node]
    assert result['unit']['one.py']['seconds'] == pytest.approx(.5)


def test_explicit_baseline_cli_never_emits_passing_coverage_for_a_failed_run(tmp_path):
    rows = diagnostic_reports()
    node = rows[0]['executions'][0]
    rows[0]['tests'][node]['call']['outcome'] = 'failed'
    rows[0]['exitstatus'] = 1
    for row in rows:
        directory = tmp_path / f"{row['suite']}-{row['index']}"
        directory.mkdir()
        (directory / 'shard.json').write_text(json.dumps(row))
    command = [sys.executable, str(BASELINE.parent.parent / 'ci_test_shards.py'), str(tmp_path)]
    output = tmp_path / 'baseline.json'
    subprocess.run([*command, 'baseline', '--total=2', '--run=https://example.test/run',
                    '--output=' + str(output)], check=True, capture_output=True, text=True)
    assert json.loads(output.read_text())['failed_cases'] == [node]
    rejected = subprocess.run([*command, 'unit', '--total=2'], capture_output=True, text=True)
    assert rejected.returncode != 0 and 'did not pass' in rejected.stderr
    assert not (tmp_path / 'unit-timings.json').exists()
    assert not (tmp_path / 'browser-timings.json').exists()


def test_killed_shard_retains_completed_phases_failure_details_and_active_call(tmp_path):
    report = tmp_path / 'evidence' / 'shard.json'
    blocked = tmp_path / 'blocked'
    case = tmp_path / 'test_receipts.py'
    case.write_text('''from pathlib import Path
import time
def test_pass():
    assert True
def test_failure():
    print("retained captured output")
    assert False, "retained assertion diagnostic"
def test_block():
    Path(__file__).with_name("blocked").write_text("call entered")
    time.sleep(300)
def test_unknown_tail():
    raise AssertionError("must remain unexecuted")
''')
    configuration = tmp_path / 'pytest.ini'
    configuration.write_text('[pytest]\n')
    env = {**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
    with (tmp_path / 'child.log').open('w') as output:
        child = subprocess.Popen([sys.executable, '-m', 'pytest', '-p', 'ci_test_shards',
            '-c', str(configuration), '--rootdir', str(tmp_path), str(case),
            '--ci-shard=1/1', '--ci-suite=unit', '--ci-shard-report=' + str(report)],
            cwd=Path(__file__).resolve().parents[1], env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while not blocked.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            assert blocked.exists(), (tmp_path / 'child.log').read_text()
            child.kill()
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
    recovered = load_report(report)
    nodes = recovered['selected']
    assert len(nodes) == 4
    assert recovered['executions'] == nodes[:3]
    assert recovered['active'] == {'nodeid': nodes[2], 'phase': 'call'}
    for node in nodes[:2]:
        assert set(recovered['tests'][node]) == {'setup', 'call', 'teardown'}
    failed = recovered['tests'][nodes[1]]['call']
    assert failed['outcome'] == 'failed'
    assert 'retained assertion diagnostic' in failed['diagnostic']
    assert 'retained captured output' in failed['captured']
    assert set(recovered['tests'][nodes[2]]) == {'setup'}
    assert nodes[3] not in recovered['tests'] and 'exitstatus' not in recovered
    with pytest.raises(ValueError, match='did not pass'):
        verify([recovered], 'unit', total=1)
    # Even a forged success cannot turn the interrupted active call into coverage.
    recovered['exitstatus'] = 0
    with pytest.raises(ValueError, match='incomplete'):
        verify([recovered], 'unit', total=1)
    with report.with_suffix('.events.jsonl').open('ab') as stream:
        stream.write(b'{"event":"partial')
    partial = load_report(report)
    assert partial['journal_incomplete'] and partial['tests'] == recovered['tests']


@pytest.mark.parametrize('change_directory', [False, True])
def test_journal_keeps_its_io_during_product_fault_injection(tmp_path, monkeypatch, change_directory):
    import ci_test_shards as journal

    monkeypatch.chdir(tmp_path)
    shard = journal.Shard(None, 1, 1, Path('evidence/shard.json'), 'unit', {})
    command = journal.pytest_cmdline_main(SimpleNamespace(
        pluginmanager=SimpleNamespace(get_plugin=lambda name: shard)))
    next(command)
    syncs = []
    synchronize = shard._sync

    def sync(fd):
        syncs.append(fd)
        synchronize(fd)

    shard._sync = sync
    product_directory = tmp_path / 'product'
    product_directory.mkdir()
    node = 'test_product.py::test_fault'
    shard.report.update(expected=[node], selected=[node], file_owners={'test_product.py': 1}, estimated_seconds=[1])
    shard.write()
    def fail(*args, **kwargs):
        raise OSError('product-only fault')
    with monkeypatch.context() as patch:
        if change_directory:
            patch.chdir(product_directory)
        patch.setattr(json, 'dumps', fail)
        patch.setattr(os, 'fsync', fail)
        assert journal._diagnostic('failure details') == 'failure details'
        shard.event('start', nodeid=node)
        for phase in ('setup', 'call', 'teardown'):
            shard.event('phase', nodeid=node, phase=phase)
            shard.event('outcome', nodeid=node, phase=phase, result={'outcome': 'passed', 'seconds': .01})
        shard.event('end', nodeid=node)
        # Every transition is visible before the terminal synchronization.
        # A process interruption must never certify these unfinished receipts.
        pending = load_report(shard.path)
        assert pending['executions'] == [node]
        assert set(pending['tests'][node]) == {'setup', 'call', 'teardown'}
        assert 'exitstatus' not in pending and syncs == []
        with pytest.raises(ValueError, match='did not pass'):
            verify([pending], 'unit', total=1)
        shard.pytest_sessionfinish(None, 0)
        # Losing the process during remaining pytest cleanup must not certify
        # the pre-cleanup finish record as complete elapsed/budget evidence.
        unfinished = load_report(shard.path)
        assert unfinished['journal_incomplete'] and 'exitstatus' not in unfinished
        with pytest.raises(ValueError, match='did not pass'):
            verify([unfinished], 'unit', total=1)
        with pytest.raises(StopIteration) as finished:
            command.send(0)
        assert finished.value.value == 0
    recovered = load_report(shard.path)
    assert verify([recovered], 'unit', total=1)['cases'] == 1
    events = [json.loads(line) for line in shard.journal.read_text().splitlines()]
    assert [event['sequence'] for event in events] == list(range(len(events)))
    assert [event['event'] for event in events[-2:]] == ['finish', 'timing']
    assert len(syncs) == 1
    # Older completed journals carry elapsed time on finish instead. Keep
    # those historical artifacts readable without requiring a timing event.
    legacy = tmp_path / 'legacy.json'
    legacy.write_bytes(shard.path.read_bytes())
    events[-2]['elapsed_seconds'] = recovered['elapsed_seconds']
    legacy.with_suffix('.events.jsonl').write_text('\n'.join(json.dumps(event) for event in events[:-1]) + '\n')
    assert verify([load_report(legacy)], 'unit', total=1)['cases'] == 1
    assert shard.path == tmp_path / 'evidence/shard.json'
    assert not (product_directory / 'evidence').exists()


def test_real_journal_append_failure_propagates_without_reusing_sequence_or_certifying_completion(tmp_path, monkeypatch):
    import ci_test_shards as journal

    shard = journal.Shard(None, 1, 1, tmp_path / 'shard.json', 'unit', {})
    shard.report.update(expected=['test_product.py::test_fault'], selected=['test_product.py::test_fault'],
                        file_owners={'test_product.py': 1}, estimated_seconds=[1])
    shard.event('start', nodeid='test_product.py::test_fault')
    open_file = Path.open

    def fail(path, *args, **kwargs):
        if path == shard.journal and args == ('ab',):
            raise OSError('actual journal append failure')
        return open_file(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', fail)
    with pytest.raises(OSError, match='actual journal append failure'):
        shard.event('phase', nodeid='test_product.py::test_fault', phase='setup')
    before = shard.journal.read_bytes()
    with pytest.raises(OSError, match='unavailable after a recording failure'):
        shard.event('end', nodeid='test_product.py::test_fault')
    shard.pytest_sessionfinish(None, 3)
    assert shard.journal.read_bytes() == before
    events = [json.loads(line) for line in before.splitlines()]
    assert [event['sequence'] for event in events] == [0]
    recovered = load_report(shard.path)
    assert recovered['journal_incomplete'] and 'exitstatus' not in recovered
    assert recovered['active'] == {'nodeid': 'test_product.py::test_fault', 'phase': 'starting'}
    with pytest.raises(ValueError, match='did not pass'):
        verify([recovered], 'unit', total=1)


def test_failed_terminal_sync_cannot_certify_a_passing_shard(tmp_path, monkeypatch):
    import ci_test_shards as journal

    shard = journal.Shard(None, 1, 1, tmp_path / 'shard.json', 'unit', {})
    node = 'test_product.py::test_fault'
    shard.report.update(expected=[node], selected=[node], file_owners={'test_product.py': 1}, estimated_seconds=[1])
    shard.event('start', nodeid=node)
    for phase in ('setup', 'call', 'teardown'):
        shard.event('outcome', nodeid=node, phase=phase, result={'outcome': 'passed', 'seconds': .01})
    shard.event('end', nodeid=node)
    def fail(fd):
        raise OSError('terminal sync failed')
    monkeypatch.setattr(shard, '_sync', fail)
    with pytest.raises(OSError, match='terminal sync failed'):
        shard.pytest_sessionfinish(None, 0)
    recovered = load_report(shard.path)
    assert recovered['journal_incomplete'] and 'exitstatus' not in recovered
    with pytest.raises(ValueError, match='did not pass'):
        verify([recovered], 'unit', total=1)


@pytest.mark.parametrize('terminal_cost', ['sync', 'report', 'source', 'cleanup'])
def test_terminal_work_is_in_recovered_elapsed_time_and_can_exceed_browser_budget(tmp_path, terminal_cost):
    # Run the actual pytest lifecycle with real journal I/O. Only the clock is
    # controlled: each selected terminal operation accounts for twenty seconds.
    (tmp_path / 'pytest.ini').write_text('[pytest]\n')
    (tmp_path / 'conftest.py').write_text('''
from types import SimpleNamespace
import pytest
import time
import ci_test_shards as journal

clock = [1000.0]
journal.time = SimpleNamespace(monotonic=lambda: clock[0], process_time=time.process_time)

@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    shard = config.pluginmanager.get_plugin('isolated-ci-shard')
    name = ''' + repr(terminal_cost) + '''
    if name == 'cleanup':
        config.add_cleanup(lambda: clock.__setitem__(0, clock[0] + 20))
    else:
        attribute = {'sync': '_sync', 'report': 'write', 'source': 'write_sources'}[name]
        original = getattr(shard, attribute)
        def charged(*args, **kwargs):
            result = original(*args, **kwargs)
            clock[0] += 20
            return result
        setattr(shard, attribute, charged)

@pytest.fixture(autouse=True)
def test_clock():
    clock[0] = 2190.0
''')
    (tmp_path / 'test_probe.py').write_text('def test_probe():\n    assert True\n')
    report = tmp_path / 'shard.json'
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-p', 'ci_test_shards', '-c', str(tmp_path / 'pytest.ini'),
         '--rootdir', str(tmp_path), str(tmp_path / 'test_probe.py'), '--ci-shard=1/1',
         '--ci-suite=browser', '--ci-shard-report=' + str(report),
         '--ci-source-report=' + str(tmp_path / 'source.json')],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
        env={**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'PYTEST_ADDOPTS': ''})
    assert result.returncode == 0, result.stdout + result.stderr
    recovered = load_report(report)
    assert recovered['elapsed_seconds'] == 1210
    # CPU timing is diagnostic only: it cannot replace charged wall time or
    # turn an over-budget browser run into a passing receipt.
    assert recovered['process_cpu_seconds'] > 0
    assert recovered['runtime']['python'] == '.'.join(map(str, sys.version_info[:3]))
    assert recovered['runtime']['cpu_count'] == os.cpu_count()
    with pytest.raises(ValueError, match='aggregate.*1200s total budget'):
        verify([recovered], 'browser', total=1)
    recovered['suite'] = 'unit'
    measured = verify([recovered], 'unit', total=1)
    assert measured['shard_seconds'] == [1210]
    assert measured['test_runner_minutes'] == pytest.approx(1210 / 60)


@pytest.mark.parametrize('fault', ['cleanup', 'timing-append'])
def test_failure_after_terminal_sync_cannot_certify_a_passing_shard(tmp_path, monkeypatch, fault):
    import ci_test_shards as journal

    shard = journal.Shard(None, 1, 1, tmp_path / 'shard.json', 'unit', {})
    command = journal.pytest_cmdline_main(SimpleNamespace(
        pluginmanager=SimpleNamespace(get_plugin=lambda name: shard)))
    next(command)
    node = 'test_product.py::test_fault'
    shard.report.update(expected=[node], selected=[node], file_owners={'test_product.py': 1}, estimated_seconds=[1])
    shard.event('start', nodeid=node)
    for phase in ('setup', 'call', 'teardown'):
        shard.event('outcome', nodeid=node, phase=phase, result={'outcome': 'passed', 'seconds': .01})
    shard.event('end', nodeid=node)
    shard.pytest_sessionfinish(None, 0)
    synchronized = shard.journal.read_bytes()
    if fault == 'cleanup':
        with pytest.raises(OSError, match='cleanup failed'):
            command.throw(OSError('cleanup failed'))
    else:
        open_file = Path.open
        def fail(path, *args, **kwargs):
            if path == shard.journal and args == ('ab',):
                raise OSError('timing append failed')
            return open_file(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'open', fail)
        with pytest.raises(OSError, match='timing append failed'):
            command.send(0)
    assert shard.journal.read_bytes() == synchronized
    recovered = load_report(shard.path)
    assert recovered['journal_incomplete'] and 'exitstatus' not in recovered
    assert set(recovered['tests'][node]) == {'setup', 'call', 'teardown'}
    with pytest.raises(ValueError, match='did not pass'):
        verify([recovered], 'unit', total=1)


def test_retained_diagnostics_are_bounded_and_keep_exception_tail():
    text = 'first line\n' + ('verbose output\n' * DIAGNOSTIC_BYTES) + 'last exception'
    retained = _diagnostic(text)
    assert len(json.dumps(retained, ensure_ascii=False).encode()) <= DIAGNOSTIC_BYTES
    assert retained.startswith('first line') and retained.endswith('last exception')


def test_source_receipt_keeps_both_sides_of_a_run_including_added_and_removed_files(tmp_path):
    import hashlib
    import ci_test_shards as journal

    (tmp_path / 'src').mkdir()
    original = tmp_path / 'src' / 'subject.py'
    original.write_bytes(b'before\n')
    removed = tmp_path / 'old_plugin.py'
    removed.write_bytes(b'old\n')
    config = SimpleNamespace(rootpath=tmp_path, invocation_params=SimpleNamespace(args=('tests/subject.py',)))
    path = tmp_path / 'evidence' / 'source.json'
    shard = journal.Shard(config, 1, 1, tmp_path / 'evidence' / 'shard.json', 'unit', {}, source_path=path)
    before = json.loads(path.read_text())
    assert before['sources']['src/subject.py'] == hashlib.sha256(b'before\n').hexdigest()
    assert 'after_sources' not in before
    original.write_bytes(b'after\n')
    removed.unlink()
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests' / 'added.py').write_bytes(b'added\n')
    shard.pytest_sessionfinish(None, 0)
    report = json.loads(path.read_text())
    assert report['sources'] == before['sources']
    assert report['after_sources'] == {
        'src/subject.py': hashlib.sha256(b'after\n').hexdigest(),
        'tests/added.py': hashlib.sha256(b'added\n').hexdigest(),
    }
    assert report['arguments'] == ['tests/subject.py']


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
    assert timing_source(Source(), {'refreshed_at': '2026-10-25T12:33:40Z'}) == {
        "timing_run": "123", "timing_artifact": "55"}


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


@pytest.mark.parametrize('measured_at,expected', [
    ('2026-10-24T12:00:00Z', True),
    ('2026-10-25T12:00:00Z', False),
    ('2026-10-25T12:00:01Z', False),
    (None, False),
])
def test_baseline_export_time_does_not_hide_a_newer_passing_measurement(measured_at, expected):
    class Source:
        repository = 'owner/repo'

        def api(self, path):
            if path == 'repos/owner/repo/actions/runs/120':
                return {'run_started_at': measured_at}
            if '/artifacts?' in path:
                return {'artifacts': [{'name': 'source-test-timings-1', 'id': 99,
                                       'expired': False, 'size_in_bytes': 100}]}
            return {'workflow_runs': [
                {'status': 'completed', 'conclusion': 'success', 'event': 'pull_request',
                 'head_repository': {'full_name': 'owner/repo'}, 'id': 123, 'run_attempt': 1,
                 'run_started_at': '2026-10-25T12:00:00Z'},
            ]}

    baseline = {'run': 'https://github.com/owner/repo/actions/runs/120',
                'refreshed_at': '2026-10-25T12:33:40Z'}
    assert timing_source(Source(), baseline) == (
        {'timing_run': '123', 'timing_artifact': '99'} if expected
        else {'timing_run': '', 'timing_artifact': ''})


def hosted_jobs(shards=2, prefix="", *, browser_shards=None):
    counts = {'unit': shards, 'browser': shards if browser_shards is None else browser_shards}
    jobs = [{"name": f"{suite} ({index}/{total})", "html_url": "https://example.test/job", "conclusion": "success",
             "created_at": "2026-10-23T12:00:00Z", "started_at": "2026-10-23T12:01:00Z",
             "completed_at": "2026-10-23T12:04:00Z", "steps": [
                 {"name": "Run the test suite", "started_at": "2026-10-23T12:02:00Z", "completed_at": "2026-10-23T12:03:00Z"}]}
            for suite, total in counts.items() for index in range(1, total + 1)]
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


def test_eight_unit_and_four_browser_metrics_require_all_twelve_runners():
    jobs = hosted_jobs(8, browser_shards=4)
    result = measure(jobs, 'abc', shards=8, browser_shards=4)
    assert result['complete']
    assert result['shards'] == 8 and result['browser_shards'] == 4
    assert result['test_runner_minutes'] == 36
    assert len(result['jobs']) == 12
    for missing in ('unit (8/8)', 'browser (4/4)'):
        assert not measure([job for job in jobs if job['name'] != missing], 'abc',
                           shards=8, browser_shards=4)['complete']
    assert not measure(jobs, 'abc', shards=8)['complete']


def test_hosted_collection_matches_each_suites_own_shard_count():
    class Source:
        repository = 'owner/repo'

        def api(self, path):
            if '/jobs?' in path:
                return {'jobs': hosted_jobs(8, browser_shards=4)}
            return {'head_sha': 'abc', 'event': 'pull_request', 'run_attempt': 1,
                    'html_url': 'https://example.test/run/123'}

    suites = {suite: {**verify(reports(total), 'unit', total=total), 'suite': suite}
              for suite, total in (('unit', 8), ('browser', 4))}
    assert collect(Source(), '123', None, 8, suites, browser_shards=4)['complete']
    suites['browser']['shards'] = 8
    assert not collect(Source(), '123', None, 8, suites, browser_shards=4)['complete']


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


def test_unit_shard_comparison_keeps_browser_shards_at_four():
    rows = measurements()
    for row in rows:
        total = 4 if row['shards'] == 1 else 8
        row.update(shards=total, browser_shards=4)
        row['suites']['unit']['shards'] = total
        row['suites']['browser']['shards'] = 4
    result = compare(rows)
    assert (result['baseline_shards'], result['candidate_shards']) == (4, 8)
    assert result['baseline_browser_shards'] == result['candidate_browser_shards'] == 4
    rows[0]['browser_shards'] = 2
    with pytest.raises(ValueError):
        compare(rows)


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
