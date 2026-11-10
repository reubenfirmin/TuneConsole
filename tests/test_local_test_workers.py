"""Verify automatic worker selection and real subprocess ownership on tiny suites."""

import fcntl
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import local_test_workers as workers


@pytest.fixture
def disable_periodic_library_sync():
    # These plugin checks construct their own isolated child lifecycles.
    pass


def config_for(tmp_path, args=("tests",), **options):
    return SimpleNamespace(
        rootpath=tmp_path, args=list(args),
        invocation_params=SimpleNamespace(dir=tmp_path),
        getoption=lambda name, default=None: options.get(name, default),
    )


@pytest.mark.parametrize("args,options,expected", [
    (("tests",), {}, True), ((".",), {}, True),
    (("tests",), {"markexpr": "browser"}, True),
    (("tests/test_one.py",), {}, False),
    (("tests/browser",), {}, False),
    (("tests",), {"keyword": "one"}, False),
    (("tests",), {"markexpr": "slow"}, False),
    (("tests",), {"ci_shard": "1/2"}, False),
    (("tests",), {"lf": True}, False),
    (("tests",), {"collectonly": True}, False),
    (("tests",), {"usepdb": True}, False),
    (("tests",), {"capture": "no"}, False),
    (("tests",), {"run_live_radio": True}, False),
    (("tests",), {"run_flatpak_build": True}, False),
])
def test_only_complete_local_suites_get_workers(tmp_path, monkeypatch, args, options, expected):
    monkeypatch.delenv("CI", raising=False)
    assert workers.full_local_run(config_for(tmp_path, args, **options)) is expected


def test_ci_is_serial_and_default_count_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("CI", "true")
    assert not workers.full_local_run(config_for(tmp_path))
    monkeypatch.setattr(os, "sched_getaffinity", lambda _: set(range(16)))
    assert workers.default_workers() == 4
    monkeypatch.setattr(os, "sched_getaffinity", lambda _: {0})
    assert workers.default_workers() == 0


@pytest.fixture
def mini_suite(tmp_path):
    return _mini_suite(tmp_path)


def _mini_suite(tmp_path):
    """Both controller and workers load the real plugins with an isolated lock."""
    repo = Path(__file__).resolve().parents[1]
    (tmp_path / "pytest.ini").write_text(
        f"[pytest]\ntestpaths = tests\npythonpath = {repo}\naddopts = -p pytest_coverage_guard\n")
    (tmp_path / "tests").mkdir()
    lock_path = tmp_path / "owner.lock"
    (tmp_path / "conftest.py").write_text(
        "import importlib.util, sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(repo)!r})\n"
        f"spec = importlib.util.spec_from_file_location('mini_machine_owner', {str(repo / 'conftest.py')!r})\n"
        "plugin = importlib.util.module_from_spec(spec)\n"
        "sys.modules[spec.name] = plugin\nspec.loader.exec_module(plugin)\n"
        f"plugin._LOCK_PATH = Path({str(lock_path)!r})\n"
        "import local_test_workers\nlocal_test_workers.default_workers = lambda: 2\n"
        "pytest_plugins = ['mini_machine_owner']\n"
    )
    for name, other in (("one", "two"), ("two", "one")):
        (tmp_path / "tests" / f"test_{name}.py").write_text(
            "import json, os, time\nfrom pathlib import Path\nimport pytest\n"
            f"NAME = {name!r}\nOTHER = {other!r}\n"
            "@pytest.mark.parametrize('case', [0, 1])\n"
            "def test_probe(case):\n"
            "    if os.environ.get('RENDEZVOUS') and case == 0:\n"
            "        Path(NAME + '.started').touch()\n"
            "        deadline = time.monotonic() + 10\n"
            "        while not Path(OTHER + '.started').exists():\n"
            "            assert time.monotonic() < deadline, 'workers did not overlap'\n"
            "            time.sleep(0.02)\n"
            "    with Path(f'{NAME}-{case}.json').open('x') as out:\n"
            "        json.dump({'pid': os.getpid(), 'threads': os.environ.get('OPENBLAS_NUM_THREADS')}, out)\n"
            "    assert not os.environ.get('FAIL_PROBE')\n"
        )

    @contextmanager
    def start(*args, **env):
        child = subprocess.Popen(
            [sys.executable, "-m", "pytest", "-p", "xdist.plugin", "-q", "--assert=plain", *args],
            cwd=tmp_path,
            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": "",
                 "CI": "", **env}, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        try:
            yield child
        finally:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            # A reaped coordinator does not establish that its worker/browser
            # group has stopped. Verify the owned group before releasing files.
            deadline = time.monotonic() + 2
            while True:
                remaining = []
                for path in Path('/proc').glob('[0-9]*/stat'):
                    try:
                        state, _, group, *_ = path.read_text().rsplit(')', 1)[1].split()
                    except FileNotFoundError:
                        continue
                    if int(group) == child.pid and state != 'Z':
                        remaining.append(int(path.parent.name))
                if not remaining:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f'Owned child group {child.pid} still running: {remaining}')
                time.sleep(.01)
            child.cleanup_receipt = {'group': child.pid, 'remaining': remaining}

    def run(*args, **env):
        with start(*args, **env) as child:
            stdout, stderr = child.communicate(timeout=30)
            completed = subprocess.CompletedProcess(child.args, child.returncode, stdout, stderr)
        completed.cleanup_receipt = child.cleanup_receipt
        return completed

    run.start = start
    return tmp_path, lock_path, run


def test_full_run_executes_every_case_once_on_parallel_file_workers(mini_suite):
    root, lock_path, run = mini_suite
    result = run(RENDEZVOUS="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 passed" in result.stdout
    records = {path.stem: json.loads(path.read_text()) for path in root.glob("*-*.json")}
    assert set(records) == {"one-0", "one-1", "two-0", "two-1"}
    assert records["one-0"]["pid"] == records["one-1"]["pid"]
    assert records["two-0"]["pid"] == records["two-1"]["pid"]
    assert records["one-0"]["pid"] != records["two-0"]["pid"]
    assert all(record["threads"] == "1" for record in records.values())
    assert json.loads(lock_path.read_text())["pid"] not in {record["pid"] for record in records.values()}


@pytest.mark.parametrize("args,env,count,locked", [
    (("tests/test_one.py",), {}, 2, False),
    (("-n", "0"), {}, 4, True),
    ((), {"CI": "true"}, 4, True),
])
def test_focused_override_and_ci_runs_remain_serial(mini_suite, args, env, count, locked):
    root, lock_path, run = mini_suite
    result = run(*args, **env)
    assert result.returncode == 0, result.stdout + result.stderr
    pids = [json.loads(path.read_text())["pid"] for path in root.glob("*-*.json")]
    assert len(pids) == count
    assert len(set(pids)) == 1
    assert lock_path.exists() is locked
    if locked:
        assert set(pids) == {json.loads(lock_path.read_text())["pid"]}


def test_explicit_workers_cannot_parallelize_a_focused_check(mini_suite):
    root, _, run = mini_suite
    result = run("tests/test_one.py", "-n", "2")
    assert result.returncode == 4
    assert "Tests must run serially" in result.stderr
    assert not list(root.glob("*-*.json"))


def test_worker_failures_fail_the_session(mini_suite):
    _, _, run = mini_suite
    result = run(FAIL_PROBE="1")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "4 failed" in result.stdout


@pytest.mark.parametrize("with_coverage", [False, True])
def test_coordinator_records_live_progress_from_every_worker(mini_suite, with_coverage):
    root, _, run = mini_suite
    for module in (root / 'tests').glob('test_*.py'):
        with module.open('a') as stream:
            stream.write('''
@pytest.fixture(autouse=True)
def sqlite_temporary_storage(request):
    import sqlite3
    from contextlib import closing
    directory = request.config._sqlite_test_storage
    with closing(sqlite3.connect('')) as connection:
        connection.execute('PRAGMA cache_size=-16')
        connection.execute('CREATE TABLE probe(value BLOB)')
        connection.executemany('INSERT INTO probe VALUES (zeroblob(65536))', [()] * 8)
        connection.commit()
        files = []
        for descriptor in Path('/proc/self/fd').iterdir():
            try:
                target = str(descriptor.readlink())
            except FileNotFoundError:
                continue
            if 'etilqs_' in target:
                files.append(target.removesuffix(' (deleted)'))
        assert files and all(Path(path).is_relative_to(directory) for path in files), files
        usage = os.statvfs(directory)
        Path(f'{NAME}-{request.node.callspec.params["case"]}-sqlite.json').write_text(json.dumps({
            'directory': str(directory), 'files': files,
            'free_bytes': usage.f_bavail * usage.f_frsize, 'free_inodes': usage.f_favail}))
''')
    progress = root / "progress.jsonl"
    options = ["--test-progress", str(progress)]
    if with_coverage:
        options += ["-n", "2", "-p", "pytest_cov.plugin", "--cov=.", "--cov-context=test", "--cov-report=",
                    "--cov-config=" + str(Path(__file__).resolve().parents[1] / "pyproject.toml")]
    result = run(*options)
    assert result.returncode == 0, result.stdout + result.stderr
    events = [json.loads(line) for line in progress.read_text().splitlines()]
    assert len([row for row in events if row["event"] == "start"]) == 4
    assert len([row for row in events if row["event"] == "end"]) == 4
    phases = [row for row in events if row["event"] == "phase"]
    assert len(phases) == 12
    assert {row["worker"] for row in phases} == {"gw0", "gw1"}
    assert all(row["outcome"] == "passed" for row in phases)
    assert events[-1]["event"] == "finish" and events[-1]["exitstatus"] == 0
    assert "start tests/test_one.py::test_probe[0]" in result.stdout
    storage = [json.loads(path.read_text()) for path in root.glob('*-sqlite.json')]
    assert len(storage) == 4 and len({row['directory'] for row in storage}) == 2
    assert all(row['free_bytes'] > 0 and row['free_inodes'] > 0 for row in storage)
    assert all(not Path(row['directory']).parent.exists() for row in storage)
    if with_coverage:
        from coverage import Coverage

        coverage = Coverage(data_file=str(root / ".coverage"))
        coverage.load()
        assert {f"tests/test_{name}.py::test_probe[{case}]|run" for name in ("one", "two") for case in (0, 1)} <= (
            coverage.get_data().measured_contexts())


def test_coverage_without_explicit_workers_keeps_the_serial_controller(mini_suite):
    root, _, run = mini_suite
    result = run("-p", "pytest_cov.plugin", "--cov=local_test_workers", "--cov-report=",
                 "--cov-config=" + str(Path(__file__).resolve().parents[1] / "pyproject.toml"))
    assert result.returncode == 0, result.stdout + result.stderr
    pids = [json.loads(path.read_text())["pid"] for path in root.glob("*-*.json")]
    assert len(pids) == 4 and len(set(pids)) == 1


def test_parallel_workers_remove_explicit_basetemp(mini_suite):
    root, _, run = mini_suite
    (root / "tests" / "test_temp.py").write_text(
        "def test_temp(tmp_path, tmp_path_factory):\n"
        "    (tmp_path / 'fixture.db').write_bytes(b'disposable')\n"
        "    shared = tmp_path_factory.mktemp('shared')\n"
        "    (shared / 'fixture.db').write_bytes(b'disposable')\n"
    )
    basetemp = root / "temp"
    result = run("--basetemp", str(basetemp))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "5 passed" in result.stdout
    assert not basetemp.exists()


@pytest.mark.parametrize('ending,exitstatus', [('passed', 0), ('failed', 1), ('interrupt', 2)])
def test_fixture_files_survive_finalizers_and_are_removed_afterward(mini_suite, ending, exitstatus):
    root, _, run = mini_suite
    (root / 'tests' / 'test_custody.py').write_text('''import json
import os
from pathlib import Path
import sqlite3
import pytest

@pytest.fixture
def owner(tmp_path):
    database = tmp_path / 'owned.sqlite3'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE retained(value)')
    try:
        yield database
    finally:
        with sqlite3.connect(database) as connection:
            assert connection.execute('SELECT count(*) FROM retained').fetchone()[0] == 0
        Path('finalized.json').write_text(json.dumps({'workspace': str(tmp_path)}))

def test_first(owner):
    assert owner.is_file()
    if os.environ['ENDING'] == 'interrupt':
        raise KeyboardInterrupt('Controlled fixture-custody interruption')
    assert os.environ['ENDING'] != 'failed', 'Controlled assertion failure'

def test_next():
    assert not Path(json.loads(Path('finalized.json').read_text())['workspace']).exists()
''')
    basetemp = root / 'custody-temp'
    result = run('tests/test_custody.py', '--basetemp', str(basetemp), ENDING=ending)
    assert result.returncode == exitstatus, result.stdout + result.stderr
    observation = json.loads((root / 'finalized.json').read_text())
    assert not Path(observation['workspace']).exists()
    assert not basetemp.exists()
    assert 'unable to open database file' not in result.stdout + result.stderr
    if ending != 'interrupt':
        expected = '2 passed' if ending == 'passed' else '1 failed, 1 passed'
        assert expected in result.stdout  # The following cleanup assertion passed.


def test_parallel_runtime_stop_keeps_storage_until_worker_finalizers_finish(tmp_path):
    with closing(_worker_custody_workflow(tmp_path)) as sequence:
        state = next(sequence)
        assert json.loads((state.root / 'coordinator.json').read_text())['pid'] == state.child.pid
        assert state.child.poll() is None

        state = next(sequence)
        started = list(state.root.glob('*.started.json'))
        assert len(started) == 2
        for path in started:
            assert (Path(json.loads(path.read_text())['workspace']) / 'owned.sqlite3').is_file()

        state = next(sequence)
        assert len(list(state.root.glob('*.finalizing'))) == 2
        assert not list(state.root.glob('*.finalized.json'))
        assert state.child.poll() is None and state.basetemp.is_dir()
        for path in started:
            assert (Path(json.loads(path.read_text())['workspace']) / 'owned.sqlite3').is_file()

        state = next(sequence)
    root, basetemp, result = state.root, state.basetemp, state.result
    assert result.returncode == 2, result.stdout + result.stderr
    started = list(root.glob('*.started.json'))
    assert len(started) == 2, result.stdout + result.stderr
    for path in started:
        observation = json.loads(path.read_text())
        assert path.with_name(path.name.replace('.started.', '.finalized.')).is_file(), result.stdout + result.stderr
        assert not Path(observation['workspace']).exists()
    assert not basetemp.exists()
    assert 'unable to open database file' not in result.stdout + result.stderr
    assert result.cleanup_receipt['remaining'] == []


def _worker_custody_workflow(root):
    root, _, run = _mini_suite(root)
    with (root / 'conftest.py').open('a') as config:
        config.write('''
import json, os, signal, time
import pytest
import pytest_time_limit

# Deadline accounting has independent owners. Install the actual monitor, then
# deliver its real signal only after the parent observes both live SQL owners.
start_timer = pytest_time_limit.SessionLimit.start_timer
def parent_gated_timer(self, delay, interval=0):
    start_timer(self, delay, interval)
    signal.setitimer(signal.ITIMER_REAL, 0)
pytest_time_limit.SessionLimit.start_timer = parent_gated_timer

@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_sessionstart(session):
    yield
    if not hasattr(session.config, 'workerinput'):
        Path('coordinator.json').write_text(json.dumps({'pid': os.getpid()}))
        deadline = time.monotonic() + 15
        while not Path('collect').exists():
            assert time.monotonic() < deadline
            time.sleep(.002)
''')
    for name in ('one', 'two'):
        (root / 'tests' / f'test_{name}.py').write_text('''import json
from pathlib import Path
import sqlite3
import time
import pytest

@pytest.fixture
def owner(tmp_path):
    database = tmp_path / 'owned.sqlite3'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE retained(value)')
    name = Path(__file__).stem
    Path(name + '.started.json').write_text(json.dumps({'workspace': str(tmp_path)}))
    try:
        yield database
    finally:
        Path(name + '.finalizing').touch()
        deadline = time.monotonic() + 15
        while not Path('release-finalizers').exists():
            assert time.monotonic() < deadline
            time.sleep(.002)
        with sqlite3.connect(database) as connection:
            assert connection.execute('SELECT count(*) FROM retained').fetchone()[0] == 0
        Path(name + '.finalized.json').write_text(json.dumps({'workspace': str(tmp_path)}))

def test_interrupted(owner):
    while True:
        time.sleep(.02)
''')
    basetemp = root / 'parallel-custody-temp'
    with run.start('--basetemp', str(basetemp), '--test-time-limit=2') as child:
        state = SimpleNamespace(root=root, basetemp=basetemp, child=child)

        def wait_for(observed):
            deadline = time.monotonic() + 15
            while not observed() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.002)
            assert observed(), f'Owned workers exited before their expected boundary: {child.poll()}'

        try:
            wait_for(lambda: (root / 'coordinator.json').exists())
            yield state
            (root / 'collect').touch()
            wait_for(lambda: len(list(root.glob('*.started.json'))) == 2)
            yield state
            os.killpg(child.pid, signal.SIGALRM)
            wait_for(lambda: len(list(root.glob('*.finalizing'))) == 2)
            yield state
            (root / 'release-finalizers').touch()
            stdout, stderr = child.communicate(timeout=15)
            state.result = subprocess.CompletedProcess(child.args, child.returncode, stdout, stderr)
        finally:
            (root / 'release-finalizers').touch()
    state.result.cleanup_receipt = child.cleanup_receipt
    yield state


def test_lingering_worker_barrier_blocks_admission_before_collection(mini_suite):
    root, lock_path, run = mini_suite
    # Simulate a worker retaining its shared lifetime lock after its owner died.
    lock_path.write_text('{"pid": 123, "command": "former coordinator"}')
    with workers.workers_lock_path(lock_path).open("a+") as barrier:
        fcntl.flock(barrier, fcntl.LOCK_SH)
        result = run("-n", "0")
        assert result.returncode == 4
        assert "Test lock busy" in result.stderr and "former coordinator" in result.stderr
        assert not list(root.glob("*-*.json"))
    result = run("-n", "0")
    assert result.returncode == 0, result.stdout + result.stderr


def test_unowned_worker_cannot_bypass_admission(tmp_path):
    config = SimpleNamespace(workerinput={})
    with pytest.raises(pytest.UsageError, match="no valid local coordinator"):
        workers.join_owner(config, tmp_path / "owner.lock")
