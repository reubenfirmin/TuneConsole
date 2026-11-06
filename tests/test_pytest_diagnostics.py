"""Live receipts and real per-test coverage survive an interrupted test call."""
import importlib.util
from contextlib import closing
import errno
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import tomllib
from xml.etree import ElementTree

from coverage import Coverage, CoverageData
import pytest




@pytest.mark.parametrize("workers", [0, 2])
@pytest.mark.parametrize("owner_exit", ["complete", "killed"])
def test_concurrent_coverage_preserves_active_destination(tmp_path, workers, owner_exit):
    repo = Path(__file__).resolve().parents[1]
    # Load the project's real early-plugin configuration, including its order
    # relative to pytest-cov, without loading the global-suite lock or fixtures.
    project = tomllib.loads((repo / "pyproject.toml").read_text())
    addopts = project["tool"]["pytest"]["ini_options"]["addopts"]
    (tmp_path / "pytest.ini").write_text(
        f"[pytest]\naddopts = {addopts}\npythonpath = {repo} .\n")
    (tmp_path / "subject.py").write_text("def choose():\n    return 10\n")
    (tmp_path / "test_owner.py").write_text('''import json
import os
from pathlib import Path
import time
from coverage import Coverage, CoverageData
import subject
def test_owner():
    assert subject.choose() == 10
    current = Coverage.current()
    current.save()
    checkpoint = CoverageData(basename=current.config.data_file)
    checkpoint.set_context('owner-checkpoint')
    checkpoint.add_lines({subject.__file__: {2}})
    checkpoint.write()
    Path('entered').write_text(json.dumps({'pid': os.getpid()}))
    deadline = time.monotonic() + 40
    while not Path('release').exists():
        assert time.monotonic() < deadline, 'owner was not released'
        time.sleep(.02)
''')
    (tmp_path / "test_other.py").write_text(
        "import subject\ndef test_other():\n    assert subject.choose() == 10\n")
    data = tmp_path / "coverage.sqlite3.focused"
    config = tmp_path / "coverage.ini"
    config.write_text(f"[run]\ncore = ctrace\ndata_file = {data}\n")
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    alias_config = tmp_path / "alias.ini"
    alias_config.write_text(f"[run]\ncore = ctrace\ndata_file = {alias / data.name}\n")
    overlapping = []
    for index, name in enumerate(["coverage.sqlite3", data.name + ".more"]):
        overlap_config = tmp_path / f"overlap-{index}.ini"
        overlap_config.write_text(f"[run]\ncore = ctrace\ndata_file = {tmp_path / name}\n")
        overlapping.append(overlap_config)
    independent_configs = []
    for index, name in enumerate(["coverage.sqlite3.independent", data.name + "-independent"]):
        private_data = tmp_path / name
        private_config = tmp_path / f"private-{index}.ini"
        private_config.write_text(f"[run]\ncore = ctrace\ndata_file = {private_data}\n")
        independent_configs.append((private_config, private_data))
    command = [sys.executable, "-m", "pytest", "-p", "pytest_cov.plugin", "-p", "xdist.plugin",
               "--cov=subject", "--cov-context=test", "--cov-report="]
    env = {**os.environ, "PYTHONPATH": str(repo), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
           "PYTEST_ADDOPTS": ""}

    def run(*args, **extra_env):
        return subprocess.run(command + list(args), cwd=tmp_path,
                              env={**env, **extra_env}, capture_output=True, text=True, timeout=10)

    output = tmp_path / "owner.log"
    run_child_probes = None
    with output.open("w") as stream:
        owner = subprocess.Popen(
            command + [f"--cov-config={config}", "-n", str(workers), "--dist=loadfile" if workers else "--dist=no",
                       "test_owner.py", "test_other.py"], cwd=tmp_path, env=env,
            stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 15
            while not (tmp_path / "entered").exists() and owner.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            assert (tmp_path / "entered").exists(), output.read_text()
            assert owner.poll() is None
            snapshots = {path: path.read_bytes() for path in tmp_path.glob(data.name + "*")}
            assert data in snapshots and len(snapshots) >= 2  # Real base and parallel SQLite data.

            conflicts = [
                ([f"--cov-config={config}"], {}),
                ([f"--cov-config={config}", "--cov-append"], {}),
                ([f"--cov-config={alias_config}"], {}),
                (["--cov-config=.coveragerc"], {"COVERAGE_FILE": str(data)}),
            ]
            # Exercise both directions of coverage's literal basename.* glob,
            # including merge/append, while real SQLite files are still owned.
            for overlap_config in overlapping:
                conflicts.extend(([f"--cov-config={overlap_config}"] + append, {})
                                 for append in [[], ["--cov-append"]])
            for options, environment in conflicts:
                started = time.monotonic()
                conflict = run(*options, "test_other.py", **environment)
                assert conflict.returncode == 4, conflict.stdout + conflict.stderr
                assert time.monotonic() - started < 5
                assert "Coverage destination busy" in conflict.stderr and str(data) in conflict.stderr
                assert f'"pid": {owner.pid}' in conflict.stderr
                assert "--cov-config" in conflict.stderr
                assert {path: path.read_bytes() for path in snapshots} == snapshots
                assert owner.poll() is None

            # Neither a different destination nor explicitly disabled coverage
            # needs to wait for the active owner or the global-suite lock.
            for private_config, private_data in independent_configs:
                independent = run(f"--cov-config={private_config}", "test_other.py")
                assert independent.returncode == 0, independent.stdout + independent.stderr
                assert private_data.exists()
            if not workers and owner_exit == 'complete':
                # Prepare checks in a distinct namespace, then run them after
                # the bounded concurrency owner exits. No browser/app fixture
                # is substituted into actual Road Trip/recap acceptance.
                (tmp_path / 'test_children.py').write_text('''import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import time
import pytest
from coverage import Coverage
import subject
from tests.browser_deadline import JourneyTimeout, WORKER, supervise_journey
from tests.process_coverage import ChildCoverage, finalize_coverage, run_spawned, switch_spawned_phase

pytestmark = pytest.mark.browser

@pytest.mark.parametrize('ending', ['complete', 'blocked', 'missing', 'corrupt', 'wrong-node', 'escaped',
    pytest.param('disabled', marks=pytest.mark.no_cover)])
def test_supervised(request, tmp_path, monkeypatch, ending):
    if os.environ.get(WORKER) == request.node.nodeid:
        from local_test_workers import full_suite
        assert not full_suite(request.config)
        current = Coverage.current()
        if ending == 'disabled':
            assert current is None
        else:
            assert current is not None
            owner = request.config._tuneconsole_coverage_owner
            assert owner['pid'] == os.getpid()
            assert Path(owner['destination']).parent.name.startswith('coverage-')
        subject.choose()
        if ending == 'blocked':
            time.sleep(60)
        if ending == 'missing':
            os._exit(0)
        if ending == 'wrong-node':
            current.switch_context('different-test|run')
            subject.choose()
            finalize_coverage(current, os.environ['TUNECONSOLE_CHILD_COVERAGE_MARKER'], request.node.nodeid, reason='probe')
            os._exit(0)
        if ending == 'corrupt':
            finalize_coverage(current, os.environ['TUNECONSOLE_CHILD_COVERAGE_MARKER'], request.node.nodeid, reason='probe')
            data = current.get_data()
            path = Path(data.data_filename())
            data.close()
            path.write_bytes(b'not a coverage database')
            os._exit(0)
        if ending == 'escaped':
            marker = Path(os.environ['TUNECONSOLE_CHILD_COVERAGE_MARKER'])
            finalize_coverage(current, marker, request.node.nodeid, reason='probe')
            receipt = json.loads(marker.read_text())
            receipt['data_files'] = [os.environ['PROBE_PARENT_COVERAGE']]
            marker.write_text(json.dumps(receipt))
            os._exit(0)
        return
    current = Coverage.current()
    if current:
        current.switch_context('outer-sentinel')
        assert subject.choose() == 10
        current.switch_context(request.node.nodeid + '|run')
        current.save()
        destination = Path(current.get_option('run:data_file'))
        snapshots = {path: path.read_bytes() for path in destination.parent.glob(destination.name + '*')}
        monkeypatch.setenv('COVERAGE_FILE', str(destination))
        monkeypatch.setenv('PROBE_PARENT_COVERAGE', str(destination))
    monkeypatch.setenv('PYTEST_ADDOPTS', '--test-lock-wait=99 --lf --cov=wrong --invalid-inherited-option')
    monkeypatch.setenv('PYTEST_XDIST_WORKER', 'inherited-worker')
    report = tmp_path / ending
    if ending == 'blocked':
        with pytest.raises(JourneyTimeout, match='exceeded 5s'):
            supervise_journey(request, 'chromium', report, seconds=5)
    elif ending == 'missing':
        with pytest.raises(AssertionError, match='no finalized coverage input'):
            supervise_journey(request, 'chromium', report, seconds=5)
    elif ending == 'corrupt':
        with pytest.raises(sqlite3.DatabaseError):
            supervise_journey(request, 'chromium', report, seconds=5)
    elif ending == 'wrong-node':
        with pytest.raises(AssertionError, match='different test node'):
            supervise_journey(request, 'chromium', report, seconds=5)
    elif ending == 'escaped':
        with pytest.raises(AssertionError, match='escaped its private namespace'):
            supervise_journey(request, 'chromium', report, seconds=5)
    else:
        assert supervise_journey(request, 'chromium', report, seconds=10) is False
    receipts = list(report.glob('coverage-*/receipt.json'))
    if ending == 'disabled':
        assert not receipts
    else:
        receipt, = [json.loads(path.read_text()) for path in receipts]
        invalid = ending in {'missing', 'corrupt', 'wrong-node', 'escaped'}
        assert receipt['coverage_valid'] == (not invalid)
        assert receipt['coverage_complete'] == (ending == 'complete')
        if invalid:
            assert {path: path.read_bytes() for path in snapshots} == snapshots
        else:
            assert receipt['run_lines'] > 0
        assert 'outer-sentinel' in current.get_data().measured_contexts()

def spawned_work(connection):
    subject.choose()
    connection.send('ready')
    assert connection.recv() == 'work'
    subject.choose()
    connection.send('done')
    assert connection.recv() == 'finish'
    subject.choose()
    connection.close()

def test_spawned(request, tmp_path, monkeypatch):
    current = Coverage.current()
    current.switch_context('spawn-outer-sentinel')
    subject.choose()
    current.switch_context(request.node.nodeid + '|run')
    capture = ChildCoverage.optional(request, tmp_path, tmp_path / 'report')
    monkeypatch.setenv('COVERAGE_FILE', current.get_option('run:data_file'))
    context = multiprocessing.get_context('spawn')
    phase, child_phase = context.Pipe()
    work, child_work = context.Pipe()
    process = context.Process(target=run_spawned, args=(capture.specification(), child_phase, spawned_work, (child_work,)))
    process.start()
    child_phase.close()
    child_work.close()
    try:
        assert work.poll(10) and work.recv() == 'ready'
        with pytest.raises(AssertionError, match='running child'):
            capture.finish(process, completed=False)
        switch_spawned_phase(phase, 'run')
        work.send('work')
        assert work.poll(5) and work.recv() == 'done'
        switch_spawned_phase(phase, 'teardown')
        work.send('finish')
        process.join(5)
        assert process.exitcode == 0
        capture.finish(process, completed=True)
        contexts = current.get_data().measured_contexts()
        assert 'spawn-outer-sentinel' in contexts
        assert {request.node.nodeid + '|' + stage for stage in ('setup', 'run', 'teardown')} <= contexts
    finally:
        if process.is_alive():
            process.kill()
        process.join(5)
        phase.close()
        work.close()
''')
                # The focused child reads this local configuration, including
                # the real early namespace guard, without suite-lock conftest.
                (tmp_path / 'pytest.ini').write_text(
                    f'[pytest]\naddopts = {addopts} -p playwright\npythonpath = {repo} .\nmarkers = browser\n')
                child_data = tmp_path / 'coverage.sqlite3.children'
                child_config = tmp_path / 'children.ini'
                child_config.write_text(f'[run]\ncore = ctrace\ndata_file = {child_data}\n')
                def run_child_probes():
                    log = tmp_path / 'children.log'
                    with log.open('w') as stream:
                        probes = subprocess.Popen(
                            [*command, '--cov-config=' + str(child_config), '-m', 'browser',
                             '--maxfail=1', 'test_children.py'], cwd=tmp_path, env=env,
                            stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
                        try:
                            assert probes.wait(timeout=45) == 0, log.read_text()
                        finally:
                            try:
                                os.killpg(probes.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            probes.wait(timeout=5)
                    child_coverage = Coverage(data_file=str(child_data))
                    try:
                        child_coverage.load()
                        contexts = child_coverage.get_data().measured_contexts()
                        assert {'outer-sentinel', 'spawn-outer-sentinel',
                                'test_children.py::test_supervised[complete]|run',
                                'test_children.py::test_supervised[blocked]|run',
                                'test_children.py::test_spawned|run'} <= contexts
                    finally:
                        child_coverage.get_data().close()
            disabled = run(f"--cov-config={config}", "--no-cov", "test_other.py")
            assert disabled.returncode == 0, disabled.stdout + disabled.stderr
            assert {path: path.read_bytes() for path in snapshots} == snapshots

            if owner_exit == "killed":
                owner.kill()
                owner.wait(timeout=5)
                if workers:
                    # Kill only the coordinator first. Its still-running worker
                    # must prevent admission/erase until that worker also exits.
                    worker_pid = json.loads((tmp_path / "entered").read_text())["pid"]
                    assert worker_pid != owner.pid
                    os.kill(worker_pid, 0)
                    for conflict_config in [config, *overlapping]:
                        conflict = run(f"--cov-config={conflict_config}", "test_other.py")
                        assert conflict.returncode == 4, conflict.stdout + conflict.stderr
                        assert "Coverage destination busy" in conflict.stderr
                        assert {path: path.read_bytes() for path in snapshots} == snapshots
            else:
                (tmp_path / "release").touch()
                assert owner.wait(timeout=10) == 0, output.read_text()
                coverage = Coverage(data_file=str(data))
                coverage.load()
                assert {"owner-checkpoint", "test_owner.py::test_owner|run",
                        "test_other.py::test_other|run"} <= coverage.get_data().measured_contexts()
        finally:
            try:
                os.killpg(owner.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            owner.wait(timeout=5)

    # Stable lock files can remain; OS ownership ends with the processes.
    deadline = time.monotonic() + 5
    while True:
        rerun = run(f"--cov-config={config}", "test_other.py")
        if rerun.returncode != 4 or time.monotonic() >= deadline:
            break
        time.sleep(.02)
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    coverage = Coverage(data_file=str(data))
    coverage.load()
    assert "test_other.py::test_other|run" in coverage.get_data().measured_contexts()
    assert "owner-checkpoint" not in coverage.get_data().measured_contexts()
    if not workers and owner_exit == "complete":
        failed_collection = run(f"--cov-config={config}", "missing_test.py")
        assert failed_collection.returncode == 4 and "not found" in failed_collection.stderr
        after_failure = run(f"--cov-config={config}", "test_other.py")
        assert after_failure.returncode == 0, after_failure.stdout + after_failure.stderr
    if run_child_probes is not None:
        run_child_probes()


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
def block():
    subject.choose(True)
    Path('entered').touch()
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
               "-q", "--test-progress=" + str(progress), "--cov=subject", "--cov-context=test",
               "--cov-report=", "--cov-config=" + str(tmp_path / "coverage.ini")]
    if interruption.startswith("deadline"):
        command.append("--test-time-limit=2")
    elif interruption.startswith("pace"):
        command.extend(["--test-runtime-target=3", "--test-runtime-slack=2"])
    with output.open("w") as stream:
        child = subprocess.Popen(command, cwd=tmp_path, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True,
            env={**os.environ, "PYTHONPATH": str(repo), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                 "PYTEST_ADDOPTS": "", "PROBE_BLOCKED_PHASE": blocked_phase,
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
            child.wait(timeout=15)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
    assert child.returncode == 2, output.read_text()
    assert (tmp_path / "test_block.cleaned").exists(), output.read_text()
    assert not (tmp_path / "test_unstarted.cleaned").exists()
    if interruption.startswith("deadline"):
        assert "--test-time-limit=2s expired" in output.read_text()
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

    # Each interrupt path must release ownership as well as save coverage.
    rerun = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "pytest_coverage_guard", "-p", "pytest_cov.plugin",
         "--cov=subject", "--cov-report=", "--cov-config=" + str(tmp_path / "coverage.ini"),
         "test_probe.py::test_choice", "-q"], cwd=tmp_path, capture_output=True, text=True, timeout=10,
        env={**os.environ, "PYTHONPATH": str(repo), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""})
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr


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
def test_runtime_target_advances_as_tests_complete(tmp_path, workers):
    repo = Path(__file__).resolve().parents[1]
    progress = tmp_path / 'progress.jsonl'
    (tmp_path / 'pytest.ini').write_text('[pytest]\n')
    (tmp_path / 'test_progressing.py').write_text(
        'import time\nimport pytest\n'
        '@pytest.mark.parametrize("case", range(4))\n'
        'def test_progress(case):\n    time.sleep(.9)\n')
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-p', 'pytest_time_limit', '-p', 'pytest_progress',
         '-p', 'xdist.plugin', '-n', str(workers), '--dist=loadfile' if workers else '--dist=no',
         '--test-runtime-target=3', '--test-runtime-slack=3', '--test-progress=' + str(progress), '-q'],
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
        env={**os.environ, 'PYTHONPATH': str(repo), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',
             'PYTEST_ADDOPTS': ''})
    assert result.returncode == 0, result.stdout + result.stderr
    receipts = [json.loads(line) for line in progress.read_text().splitlines()]
    assert len([row for row in receipts if row['event'] == 'end']) == 4
    assert receipts[-1]['event'] == 'finish' and receipts[-1]['exitstatus'] == 0
    assert receipts[-1]['elapsed_seconds'] > 3
    assert not any(row['event'] == 'pace_stop' for row in receipts)


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
