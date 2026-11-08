"""Live receipts and real per-test coverage survive an interrupted test call."""
import importlib.util
from contextlib import ExitStack, closing
import errno
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from xml.etree import ElementTree

from coverage import Coverage, CoverageData
import pytest


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    # These plugin/process checks do not construct the web application.
    pass


def overlap_module():
    path = Path(__file__).resolve().parents[1] / "scripts/test-coverage-overlap.py"
    spec = importlib.util.spec_from_file_location("test_coverage_overlap", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_overlap_separates_fixture_work_and_preserves_parameter_identity():
    data = {"files": {"subject.py": {"contexts": {
        "1": [""], "2": ["a[x|y]|setup", "b|setup"],
        "3": ["a[x|y]|run", "b|run", "b|teardown"],
        "4": ["a[x|y]|run", "b|run"], "5": ["c|run"],
    }}}}
    progress = [{"event": "start", "nodeid": "b"},
                {"event": "phase", "nodeid": "b", "phase": "setup", "seconds": 12, "outcome": "passed"},
                {"event": "phase", "nodeid": "b", "phase": "call", "seconds": 4, "outcome": "failed"}]
    result = overlap_module().summarize(data, progress)
    assert result["identical_line_groups"] == [["a[x|y]", "b"]]
    assert result["observed_lines"] == 3 and result["shared_lines"] == 2
    rows = {row["nodeid"]: row for row in result["tests"]}
    assert rows["a[x|y]"]["lines"] == 2 and rows["a[x|y]"]["exclusive_lines"] == 0
    assert rows["c"]["exclusive_lines"] == 1
    assert rows["b"]["outcomes"] == {"setup": "passed", "call": "failed"} and not rows["b"]["ended"]
    assert rows["b"]["total_seconds"] == 16 and rows["b"]["selected_seconds"] == 4
    assert overlap_module().summarize(data, progress, phase="setup")["tests"][0]["selected_seconds"] == 12
    assert result["exitstatus"] is None
    assert overlap_module().summarize(data, phase="setup")["observed_lines"] == 1
    assert overlap_module().summarize(data, phase="all")["observed_lines"] == 4
    group, = result["identical_line_group_costs"]
    assert group == {"tests": ["a[x|y]", "b"], "lines": 2,
                     "selected_seconds": 4, "seconds_above_fastest": 4}
    files = {row["file"]: row for row in result["files"]}
    assert files["b"]["setup_seconds"] == 12 and files["b"]["call_seconds"] == 4
    audit = overlap_module().audit_collection(result, ["a[x|y]", "b", "c"])
    assert not audit["complete"] and audit["expected_tests"] == 3 and audit["ended_tests"] == 0
    assert audit["not_started"] == ["a[x|y]", "c"] and audit["incomplete"] == ["b"]
    assert audit["unexpected"] == []
    complete = overlap_module().summarize(data, [{"event": "end", "nodeid": node}
                                               for node in ["a[x|y]", "b", "c"]])
    assert overlap_module().audit_collection(complete, ["a[x|y]", "b", "c"])["complete"]
    assert not overlap_module().audit_collection(complete, ["b", "c"])["complete"]
    nearest = overlap_module().summarize(data, progress, neighbors=1)["expensive_test_neighbors"]
    expensive, = nearest
    assert expensive["nodeid"] == "b" and expensive["selected_seconds"] == 4
    match, = expensive["nearest"]
    assert match["nodeid"] == "a[x|y]" and match["intersection_lines"] == 2
    assert match["target_fraction"] == match["jaccard"] == 1


@pytest.mark.parametrize("contexts", [{}, {"1": [""]}, {"1": ["unlabelled"]},
    {"1": ["tests/browser/test_road_trip_browser.py::test_build_curate_then_save_road_trip_recipe[chromium]|setup"]}])
def test_overlap_requires_per_test_contexts(tmp_path, monkeypatch, capsys, contexts):
    source = tmp_path / "coverage.json"
    source.write_text(json.dumps({"files": {"subject.py": {"contexts": contexts}}}))
    destination = tmp_path / "overlap.json"
    command = ["test-coverage-overlap.py", str(source), "--output", str(destination)]
    workflow = next((context.removesuffix('|setup') for values in contexts.values()
                     for context in values if context.endswith('|setup')), None)
    if workflow:
        # A passing outer receipt with setup-only child lines remains a gap
        # for the real run workflow, rather than a complete coverage claim.
        progress = tmp_path / 'progress.jsonl'
        progress.write_text('\n'.join(json.dumps(row) for row in [
            {'event': 'start', 'nodeid': workflow},
            {'event': 'phase', 'nodeid': workflow, 'phase': 'call', 'seconds': 1, 'outcome': 'passed'},
            {'event': 'end', 'nodeid': workflow}]) + '\n')
        monkeypatch.setattr(sys, 'argv', [*command, '--progress', str(progress)])
        assert overlap_module().main() == 1
        report = json.loads(destination.read_text())
        assert not report['child_workflow_coverage']['complete']
        assert report['child_workflow_coverage']['without_run_phase_lines'] == [workflow]
        row, = report['tests']
        assert row['ended'] and row['outcomes']['call'] == 'passed' and row['lines'] == 0
        events = [json.loads(line) for line in progress.read_text().splitlines()]
        fixture_lines = {'files': {'tests/test_helper.py': {'contexts': {'1': [workflow + '|run']}}}}
        assert not overlap_module().summarize(fixture_lines, events)['child_workflow_coverage']['complete']
        production = {'files': {'src/yt_playlist/web/routes/road_trip.py': {'contexts': {'1': [workflow + '|run']}}}}
        assert overlap_module().summarize(production, events)['child_workflow_coverage']['complete']
        assert not overlap_module().summarize(production, events[:-1])['child_workflow_coverage']['complete']
        assert not overlap_module().summarize(json.loads(source.read_text()), events, phase='all')['child_workflow_coverage']['complete']
        return
    monkeypatch.setattr(sys, "argv", command)
    with pytest.raises(SystemExit) as stopped:
        overlap_module().main()
    assert stopped.value.code == 2
    assert "--cov-context=test" in capsys.readouterr().err
    assert not destination.exists()


@pytest.mark.parametrize("workers", [0, 2])
@pytest.mark.parametrize("ending", ["complete", "interrupt", "kill"])
def test_failure_details_are_live_and_survive_interruption(tmp_path, workers, ending):
    from ci_test_shards import DIAGNOSTIC_BYTES

    repo = Path(__file__).resolve().parents[1]
    progress, output = tmp_path / "progress.jsonl", tmp_path / "console.log"
    junit = tmp_path / "final.xml"
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_probe.py").write_text('''import time
from pathlib import Path
import pytest

PAYLOAD = "詳細🌿\\t" * 20000

def fail():
    print("capture-head\\n" + PAYLOAD + "\\ncapture-tail")
    raise AssertionError("failure-head\\n" + PAYLOAD + "\\nfailure-tail")

@pytest.fixture
def broken(request):
    phase = request.param
    if phase == "setup":
        fail()
    yield phase
    if phase == "teardown":
        fail()

def test_pass():
    assert True

@pytest.mark.parametrize("broken", PHASES, indirect=True)
def test_failure(broken):
    if broken == "call":
        fail()

def test_block():
    Path("blocked").touch()
    while not Path("release").exists():
        time.sleep(.02)
'''.replace("PHASES", repr({
        'complete': ['setup', 'call', 'teardown'],
        'interrupt': ['call', 'teardown', 'setup'],
        'kill': ['teardown', 'setup', 'call'],
    }[ending])), encoding="utf-8")
    command = [
        sys.executable, "-m", "pytest", "-p", "pytest_progress", "-p", "xdist.plugin",
        "-n", str(workers), "--dist=loadfile" if workers else "--dist=no", "-q",
        "--test-progress=" + str(progress), "--junitxml=" + str(junit),
        "-o", "junit_logging=all",
    ]
    with output.open("w", encoding="utf-8") as stream:
        child = subprocess.Popen(
            command, cwd=tmp_path, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True,
            env={**os.environ, "PYTHONPATH": str(repo), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                 "PYTEST_ADDOPTS": ""})
        try:
            deadline = time.monotonic() + 15
            while child.poll() is None and time.monotonic() < deadline:
                if (tmp_path / "blocked").exists() and progress.exists():
                    # Ignore a partial final append while the coordinator writes.
                    rows = [json.loads(line) for line in progress.read_bytes().split(b"\n")[:-1]]
                    failures = [row for row in rows if row["event"] == "phase" and row["outcome"] == "failed"]
                    console = output.read_text(encoding="utf-8")
                    if len(failures) == 3 and "capture-tail" in console:
                        break
                time.sleep(.02)
            else:
                pytest.fail("Failure details were unavailable during the blocked test:\n" + output.read_text(encoding="utf-8"))

            assert child.poll() is None
            assert not any(row["event"] == "finish" for row in rows)
            assert {failure['phase'] for failure in failures} == {'setup', 'call', 'teardown'}
            for failure in failures:
                phase = failure['phase']
                assert failure["nodeid"] == f"test_probe.py::test_failure[{phase}]"
                assert failure["worker"].startswith("gw") if workers else failure["worker"] == "serial"
                assert "raise AssertionError" in failure["diagnostic"]
                for field, prefix in (("diagnostic", "failure"), ("captured", "capture")):
                    retained = failure[field]
                    assert prefix + "-head" in retained and prefix + "-tail" in retained
                    assert "[diagnostic truncated]" in retained
                    assert len(json.dumps(retained, ensure_ascii=False).encode("utf-8")) <= DIAGNOSTIC_BYTES
                    assert retained in console
                assert "Captured stdout " + phase in failure["captured"]
                assert f"phase test_probe.py::test_failure[{phase}] {phase} failed" in console
            assert all("diagnostic" not in row and "captured" not in row
                       for row in rows if row["event"] == "phase" and row["outcome"] == "passed")
            before = progress.read_bytes()
            if ending == "complete":
                (tmp_path / "release").touch()
            else:
                os.killpg(child.pid, signal.SIGINT if ending == "interrupt" else signal.SIGKILL)
            child.wait(timeout=15)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)

    assert progress.read_bytes().startswith(before)
    retained = [json.loads(line) for line in progress.read_bytes().split(b"\n")[:-1]]
    assert all(failure in retained for failure in failures)
    if ending == "kill":
        assert child.returncode == -signal.SIGKILL
        assert not any(row["event"] == "finish" for row in retained)
    else:
        assert child.returncode == (1 if ending == "complete" else 2), output.read_text(encoding="utf-8")
        assert retained[-1]["event"] == "finish"
    if ending == "complete":
        report = ElementTree.parse(junit)
        payload = "詳細🌿\t" * 20000
        for phase in ('setup', 'call', 'teardown'):
            case = report.find(f".//testcase[@name='test_failure[{phase}]']")
            assert case is not None
            full_failure = case.find("failure" if phase == "call" else "error")
            assert full_failure is not None
            assert payload in full_failure.text
            assert "[diagnostic truncated]" not in full_failure.text
            assert any(payload in (capture.text or "") for capture in case.findall("system-out"))


@pytest.mark.parametrize("workers", [0, 2])
@pytest.mark.parametrize("interruption,blocked_phase", [
    ("signal", "call"), ("deadline", "call"), ("deadline", "setup"),
    ("pace", "call"), ("pace", "setup"), ("pace-sqlite", "call"),
    ("pace-coverage-lock", "call"), ("deadline-coverage-lock", "call"),
    ("pace-storage", "call")])
def test_progress_and_coverage_survive_interrupt_after_live_test_start(tmp_path, workers, interruption, blocked_phase):
    repo = Path(__file__).resolve().parents[1]
    progress, output = tmp_path / "progress.jsonl", tmp_path / "console.log"
    (tmp_path / "pytest.ini").write_text("[pytest]\npythonpath = .\ntmp_path_retention_policy = failed\n")
    (tmp_path / "coverage.ini").write_text(f"[run]\ncore = ctrace\ndata_file = {tmp_path / '.coverage'}\n")
    (tmp_path / "subject.py").write_text(
        "def prepare():\n    return 10\n"
        "def choose(value):\n    if value:\n        return 'yes'\n    return 'no'\n")
    if interruption == 'pace-storage':
        # Fail only the monitor's stop publication. Real workers, teardown,
        # progress and coverage still have space, without filling a filesystem.
        (tmp_path / 'conftest.py').write_text('''import errno
from pathlib import Path
def pytest_sessionstart(session):
    original = Path.write_text
    def write(path, *args, **kwargs):
        if path.name == 'state.pending' and path.parent.name.startswith('runtime-monitor'):
            raise OSError(errno.ENOSPC, 'Controlled exhausted storage')
        return original(path, *args, **kwargs)
    Path.write_text = write
''')
    (tmp_path / "test_probe.py").write_text('''import os
import time
import sqlite3
import signal
from contextlib import closing
from pathlib import Path
import pytest
import subject
@pytest.fixture
def prepared():
    return subject.prepare()
@pytest.fixture(autouse=True)
def cleanup(request, tmp_path):
    yield
    Path(request.node.name + '.cleaned').touch()
    monitor = request.config.pluginmanager.getplugin('test-session-time-limit')
    if monitor is not None and monitor.interruption:
        Path(request.node.name + '.stop').write_text(monitor.interruption)
def block():
    subject.choose(True)
    Path('entered').touch()
    if os.environ.get('PROBE_DEADLINE') == '1':
        # Let the parent inspect the live blocked phase, then deliver the real
        # alarm without spending two seconds on an intentionally idle child.
        while not Path('expire').exists():
            time.sleep(.005)
        signal.setitimer(signal.ITIMER_REAL, .01)
    if os.environ.get('PROBE_COVERAGE_LOCK') == '1':
        from coverage import Coverage
        collector = Coverage.current()._collector
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
        try:
            time.sleep(4)
            collector.lock_data()
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous)
            finally:
                collector.unlock_data()
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)
    if os.environ.get('PROBE_SQLITE') == '1':
        def progress():
            time.sleep(300)
            return 0
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.set_progress_handler(progress, 1)
            conn.execute('SELECT 1').fetchone()
        return
    time.sleep(300)
@pytest.fixture
def blocked():
    if os.environ['PROBE_BLOCKED_PHASE'] == 'setup':
        block()
@pytest.mark.parametrize('value', [True, False])
def test_choice(prepared, value):
    assert subject.choose(value) == ('yes' if value else 'no')
def test_failure():
    assert subject.choose(False) == 'wrong'
def test_block(blocked):
    block()
def test_unstarted():
    assert False
''')
    command = [sys.executable, "-m", "pytest", "-p", "pytest_coverage_guard", "-p", "pytest_progress", "-p", "pytest_time_limit", "-p", "pytest_cov.plugin",
               "-p", "xdist.plugin", "-n", str(workers), "--dist=" + ("loadfile" if workers else "no"),
               "-q", "--tb=no", "--test-progress=" + str(progress), "--cov=subject", "--cov-context=test",
               "--cov-report=", "--cov-config=" + str(tmp_path / "coverage.ini"),
               "--fixture-root=" + str(tmp_path / "fixtures")]
    if interruption.startswith("deadline"):
        command.append("--test-time-limit=2")
    elif interruption.startswith("pace"):
        command.extend(["--test-runtime-target=3", "--test-runtime-slack=2"])
    with output.open("w") as stream:
        child = subprocess.Popen(command, cwd=tmp_path, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True,
            env={**os.environ, "PYTHONPATH": str(repo), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                 "PYTEST_ADDOPTS": "", "PROBE_BLOCKED_PHASE": blocked_phase,
                 "PROBE_DEADLINE": str(int(interruption == 'deadline')),
                 "PROBE_SQLITE": str(int(interruption == 'pace-sqlite')),
                 "PROBE_COVERAGE_LOCK": str(int(interruption.endswith('coverage-lock')))})
        try:
            deadline = time.monotonic() + 15
            while not (tmp_path / "entered").exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            assert (tmp_path / "entered").exists(), output.read_text()
            # Both terminal and JSONL must already be readable while the call
            # is still blocked, rather than only after a session finishes.
            assert "start test_probe.py::test_block" in output.read_text()
            live = [json.loads(line) for line in progress.read_text().splitlines()]
            assert live[-1]["event"] == ("phase" if blocked_phase == "call" else "start")
            assert live[-1]["nodeid"].endswith("test_block")
            if interruption == "signal":
                os.killpg(child.pid, signal.SIGINT)
            elif interruption == 'deadline':
                (tmp_path / 'expire').touch()
            child.wait(timeout=15)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
    assert child.returncode == 2, output.read_text()
    assert (tmp_path / "test_block.cleaned").exists(), output.read_text()
    assert not (tmp_path / "test_unstarted.cleaned").exists()
    if interruption.startswith("deadline"):
        assert '--test-time-limit=2s expired' in (tmp_path / 'test_block.stop').read_text()
    elif interruption.startswith("pace"):
        assert "behind the target line" in output.read_text()
    # Match the child's isolated config: the project's source filter excludes
    # subject.py when this parent exports the saved contexts to JSON.
    coverage = Coverage(data_file=str(tmp_path / ".coverage"), config_file=str(tmp_path / "coverage.ini"))
    coverage.load()
    contexts = coverage.get_data().measured_contexts()
    assert {"test_probe.py::test_choice[True]|run", "test_probe.py::test_choice[False]|run",
            "test_probe.py::test_choice[True]|setup", "test_probe.py::test_failure|run"} <= contexts, output.read_text()
    assert not any("unstarted" in context for context in contexts)
    report = tmp_path / "coverage.json"
    coverage.json_report(outfile=str(report), show_contexts=True)
    receipts = [json.loads(line) for line in progress.read_text().splitlines()]
    collections = [row for row in receipts if row["event"] == "collection"]
    assert len(collections) == 1 and collections[0]["tests"] == 5 and collections[0]["files"] == 1
    result = overlap_module().summarize(json.loads(report.read_text()), receipts)
    assert overlap_module().summarize(overlap_module().load_coverage(tmp_path / ".coverage"), receipts) == result
    assert result["exitstatus"] == 2 and result["ended_tests"] == 3
    assert result["observed_tests"] == 4
    assert any(row["outcomes"].get("call") == "failed" for row in result["tests"])
    if interruption == 'pace-storage':
        assert 'cannot publish runtime stop' in output.read_text()
        assert not any(row['event'] == 'pace_stop' for row in receipts)
    elif interruption.startswith("pace"):
        stopped, = [row for row in receipts if row["event"] == "pace_stop"]
        assert stopped['completed_tests'] == 3 and stopped['selected_tests'] == 5
        assert stopped['lag_seconds'] > stopped['slack_seconds'] == 2
        active, = stopped['active']
        assert active['nodeid'] == 'test_probe.py::test_block'
        assert active['phase'] == blocked_phase
        assert any(row['nodeid'].endswith('test_failure') for row in stopped['slowest'])

    # Claim the actual destination after all child processes have exited. This
    # exercises both real ownership locks without starting an unrelated suite.
    from pytest_coverage_guard import _reserve

    with ExitStack() as owner:
        config = SimpleNamespace(add_cleanup=owner.callback)
        assert _reserve(config, (tmp_path / '.coverage').resolve()) is False
        assert config._tuneconsole_coverage_owner['pid'] == os.getpid()


@pytest.mark.parametrize('monitor', ['pace', 'deadline'])
def test_runtime_monitor_defers_while_progress_writer_is_busy(tmp_path, monkeypatch, monitor):
    from types import SimpleNamespace
    from pytest_progress import Progress
    from pytest_time_limit import RuntimeTarget, SessionLimit

    plugins = {}
    config = SimpleNamespace(pluginmanager=SimpleNamespace(getplugin=plugins.get))
    output = []

    class InterruptedWriter:
        busy = False

        def write(self, value):
            assert not self.busy, 'reentrant progress write'
            self.busy = True
            try:
                limit.expired(signal.SIGALRM, None)
                output.append(value)
            finally:
                self.busy = False

        def flush(self):
            pass

    progress = Progress(config, InterruptedWriter())
    plugins['live-test-progress'] = progress
    limit = RuntimeTarget(config, 1, 0) if monitor == 'pace' else SessionLimit(config, 1)
    monkeypatch.setattr('pytest_time_limit.coverage_data_locked', lambda: False)
    monkeypatch.setattr(signal, 'setitimer', lambda *args: None)
    if monitor == 'pace':
        limit.started = time.monotonic() - 10
        limit.selected = 10
        limit.state_path = tmp_path / 'state.json'
        limit.state_path.write_text('{"stop":false}')
    progress.emit('start', nodeid='test_case')
    assert [json.loads(row)['event'] for row in output] == ['start']
    assert limit.interruption is None
    assert not progress.emitting

    # Deferral must preserve the stop: the next idle tick still interrupts,
    # with exactly one pace receipt and a readable original receipt.
    with pytest.raises(KeyboardInterrupt):
        limit.expired(signal.SIGALRM, None)
    assert [json.loads(row)['event'] for row in output] == (
        ['start', 'pace_stop'] if monitor == 'pace' else ['start'])
    if monitor == 'pace':
        assert json.loads(limit.state_path.read_text())['stop'] is True


@pytest.mark.parametrize('workers', [0, 2])
def test_runtime_target_advances_as_tests_complete(tmp_path_factory, monkeypatch, workers):
    from types import SimpleNamespace
    from pytest_time_limit import RuntimeTarget

    clock, timers = [100.], []
    config = SimpleNamespace(pluginmanager=SimpleNamespace(getplugin=lambda name: None),
        _tmp_path_factory=tmp_path_factory, getoption=lambda name, **kwargs: workers)
    monitor = RuntimeTarget(config, .8, .5)
    monkeypatch.setattr('pytest_time_limit._monotonic', lambda: clock[0])
    monkeypatch.setattr(monitor, 'start_timer', lambda *args: timers.append(args))
    monkeypatch.setattr(monitor, 'interrupt', lambda: pytest.fail('Completed tests must advance the target'))
    monitor.pytest_sessionstart(None)
    ids = [f'test_progressing.py::test_progress[{i}]' for i in range(4)]
    monitor.pytest_collection_finish(SimpleNamespace(items=ids))
    if workers:
        assert monitor.selected is None
        monitor.pytest_xdist_node_collection_finished(None, ids)
        monitor.pytest_xdist_node_collection_finished(None, ids)
    assert monitor.selected == 4 and timers == [(1, 1)]
    for nodeid in ids:
        monitor.pytest_runtest_logstart(nodeid, None)
        clock[0] += .3
        monitor.pytest_runtest_logreport(SimpleNamespace(nodeid=nodeid, when='call', duration=.3))
        monitor.pytest_runtest_logfinish(nodeid, None)
        monitor.expired(signal.SIGALRM, None)
    assert clock[0] - monitor.started > monitor.target
    assert monitor.completed == set(ids) and not monitor.active
    assert monitor.interruption is None
    assert json.loads(monitor.state_path.read_text()) == {'stop': False}


def test_runtime_stop_storage_failure_interrupts_coordinator_and_workers(tmp_path, monkeypatch):
    import errno
    from types import SimpleNamespace
    from pytest_time_limit import RuntimeTarget

    config = SimpleNamespace(pluginmanager=SimpleNamespace(getplugin=lambda name: None))
    coordinator = RuntimeTarget(config, 1, 0)
    coordinator.started = time.monotonic() - 10
    coordinator.selected = 10
    coordinator.state_path = tmp_path / 'state.json'
    coordinator.state_path.write_text('{"stop":false}')
    original = Path.write_text

    def no_space(path, *args, **kwargs):
        if path == coordinator.state_path.with_suffix('.pending'):
            raise OSError(errno.ENOSPC, 'Controlled exhausted storage')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', no_space)
    monkeypatch.setattr(signal, 'setitimer', lambda *args: None)
    monkeypatch.setattr('pytest_time_limit.coverage_data_locked', lambda: False)
    with pytest.raises(KeyboardInterrupt, match='cannot publish runtime stop'):
        coordinator.expired(signal.SIGALRM, None)
    assert not coordinator.state_path.exists()
    assert coordinator.interruption is not None
    worker = RuntimeTarget(SimpleNamespace(workerinput={}, pluginmanager=config.pluginmanager), 1, 0)
    worker.state_path = coordinator.state_path
    with pytest.raises(KeyboardInterrupt, match='Runtime monitor state unavailable'):
        worker.expired(signal.SIGALRM, None)
    assert worker.interruption is not None


@pytest.mark.parametrize('damage', ['truncated', 'unrelated', 'schema'])
def test_first_failure_finalizes_per_test_coverage(tmp_path, monkeypatch, damage):
    repo = Path(__file__).resolve().parents[1]
    (tmp_path/'pytest.ini').write_text('[pytest]\npythonpath = .\n')
    (tmp_path/'subject.py').write_text('def choose():\n    return 10\n')
    # Python's tracer can record line zero for a genuinely empty package.
    (tmp_path/'empty').mkdir()
    (tmp_path/'empty'/'__init__.py').write_text('')
    (tmp_path/'test_probe.py').write_text(
        'import subject, empty\ndef test_failure():\n    assert subject.choose() == 11\n'
        'def test_unstarted():\n    assert False\n')
    data = tmp_path/'coverage.sqlite3'
    (tmp_path/'coverage.ini').write_text(f'[run]\ncore = ctrace\ndata_file = {data}\n')
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-p', 'pytest_coverage_guard', '-p', 'pytest_progress', '-p', 'pytest_cov.plugin',
         '--test-progress='+str(tmp_path/'progress.jsonl'), '--cov=subject', '--cov=empty', '--cov-context=test',
         '--cov-report=', '--cov-config='+str(tmp_path/'coverage.ini'), '--maxfail=1', '-q'],
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
        env={**os.environ, 'PYTHONPATH': str(repo), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',
             'PYTEST_ADDOPTS': ''})
    assert result.returncode == 1, result.stdout+result.stderr
    assert data.exists(), result.stdout+result.stderr
    coverage = Coverage(data_file=str(data))
    coverage.load()
    assert 'test_probe.py::test_failure|run' in coverage.get_data().measured_contexts()
    assert not any('unstarted' in context for context in coverage.get_data().measured_contexts())
    filename = next(name for name in coverage.get_data().measured_files() if name.endswith('subject.py'))
    coverage.get_data().close()

    # A controlled second worker contributes a distinct context and source line.
    good = tmp_path / 'coverage.sqlite3.good-worker'
    worker = CoverageData(basename=str(good))
    try:
        worker.set_context('test_probe.py::test_other_worker|run')
        worker.add_lines({filename: {1}})
        worker.write()
    finally:
        worker.close()

    # Model a damaged worker after storage failure without consuming disk space.
    damaged = tmp_path / 'coverage.sqlite3.worker'
    if damage == 'truncated':
        damaged.write_bytes(data.read_bytes()[:128])
    elif damage == 'unrelated':
        with closing(sqlite3.connect(damaged)) as conn:
            conn.execute('CREATE TABLE unrelated (value TEXT)')
            conn.commit()
    else:
        damaged.write_bytes(data.read_bytes())
        with closing(sqlite3.connect(damaged)) as conn:
            conn.execute('UPDATE coverage_schema SET version=999')
            conn.commit()
    progress = tmp_path / 'progress.jsonl'
    originals = {path: path.read_bytes() for path in (data, good, damaged, progress)}
    destination = tmp_path / 'recovered.json'
    arguments = ['test-coverage-overlap.py', str(data), '--coverage-input', str(good),
                 '--coverage-input', str(damaged), '--coverage-input', str(good),
                 '--progress', str(progress), '--collection', str(progress), '--output', str(destination)]
    monkeypatch.setattr(sys, 'argv', arguments)
    module = overlap_module()
    assert module.main() == 1
    recovered = json.loads(destination.read_text())
    assert recovered['coverage_inputs']['accepted'] == [str(data), str(good)]
    rejected, = recovered['coverage_inputs']['excluded']
    assert rejected['path'] == str(damaged) and rejected['error']
    assert not recovered['coverage_inputs']['all_inputs_valid']
    assert 'cannot be certified' in recovered['scope']
    assert recovered['observed_lines'] == 2
    assert recovered['collection_audit']['not_started'] == ['test_probe.py::test_unstarted']
    assert not recovered['collection_audit']['complete']
    assert recovered['collection_audit']['unexpected'] == ['test_probe.py::test_other_worker']
    retained = destination.read_bytes()

    # A single invalid input is still an error, not an empty successful report.
    monkeypatch.setattr(sys, 'argv', [arguments[0], str(damaged), '--output', str(destination)])
    with pytest.raises(SystemExit) as invalid:
        module.main()
    assert invalid.value.code == 2 and destination.read_bytes() == retained
    for path in (data, progress):
        monkeypatch.setattr(sys, 'argv', [*arguments[:-1], str(path)])
        with pytest.raises(SystemExit) as overwrite:
            module.main()
        assert overwrite.value.code == 2

    # Even a partial ENOSPC write preserves the prior report and original data.
    write_text = Path.write_text
    def no_space(path, text, *args, **kwargs):
        if path.name == 'report.json' and path.parent.name.startswith('.coverage-report-'):
            write_text(path, 'partial')
            raise OSError(errno.ENOSPC, 'No space left on device')
        return write_text(path, text, *args, **kwargs)

    monkeypatch.setattr(sys, 'argv', [arguments[0], str(data), '--output', str(destination)])
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'write_text', no_space)
        with pytest.raises(OSError) as full:
            module.main()
    assert full.value.errno == errno.ENOSPC
    assert destination.read_bytes() == retained
    assert not list(tmp_path.glob('.coverage-report-*'))
    assert all(path.read_bytes() == content for path, content in originals.items())
