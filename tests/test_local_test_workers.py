"""Verify automatic worker selection and real subprocess ownership on tiny suites."""

import fcntl
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

    def run(*args, **env):
        child = subprocess.Popen(
            [sys.executable, "-m", "pytest", "-p", "xdist.plugin", "-q", *args],
            cwd=tmp_path,
            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": "",
                 "CI": "", **env}, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        completed = None
        try:
            stdout, stderr = child.communicate(timeout=30)
            completed = subprocess.CompletedProcess(child.args, child.returncode, stdout, stderr)
            return completed
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
            if completed is not None:
                completed.cleanup_receipt = {'group': child.pid, 'remaining': remaining}

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


def test_parallel_runtime_stop_keeps_storage_until_worker_finalizers_finish(mini_suite):
    root, _, run = mini_suite
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
        time.sleep(.2)
        with sqlite3.connect(database) as connection:
            assert connection.execute('SELECT count(*) FROM retained').fetchone()[0] == 0
        Path(name + '.finalized.json').write_text(json.dumps({'workspace': str(tmp_path)}))

def test_interrupted(owner):
    time.sleep(10)
''')
    basetemp = root / 'parallel-custody-temp'
    result = run('--basetemp', str(basetemp), '--test-time-limit=2')
    assert result.returncode == 2, result.stdout + result.stderr
    started = list(root.glob('*.started.json'))
    assert len(started) == 2, result.stdout + result.stderr
    for path in started:
        observation = json.loads(path.read_text())
        assert path.with_name(path.name.replace('.started.', '.finalized.')).is_file(), result.stdout + result.stderr
        assert not Path(observation['workspace']).exists()
    assert not basetemp.exists()
    assert 'unable to open database file' not in result.stdout + result.stderr


@pytest.mark.parametrize('monitor', [
    'pace', 'deadline', 'interrupt', 'coverage', 'setup', 'teardown',
])
def test_protocol_interrupt_keeps_live_server_storage_and_capture(mini_suite, monitor, request):
    root, _, run = mini_suite
    browser = monitor.startswith('browser-')
    monitor = monitor.removeprefix('browser-')
    # Unlike a direct exception inside a server context, this interrupts the
    # actual worker protocol and drains fixtures before pytest closes capture.
    (root / 'server_probe.py').write_text('''import json
import os
from pathlib import Path
import socket
import threading
import time
import traceback
from urllib.request import urlopen

from fastapi import FastAPI
import pytest
import uvicorn

from tests.live_server import LiveServer
from yt_playlist.core.store import Store

@pytest.fixture
def owner(tmp_path, request):
    owner_started = time.monotonic()
    phase_times = {}
    name = Path(request.node.path).stem
    page = request.getfixturevalue('page') if os.environ.get('USE_BROWSER') else None
    phase_times['browser_ready'] = time.monotonic() - owner_started
    store = Store(tmp_path / 'owned.sqlite3')
    store.init_schema()
    phase_times['schema_ready'] = time.monotonic() - owner_started
    with store.conn:
        store.conn.execute('CREATE TABLE retained(value)')
    native = tmp_path / 'response.evidence'
    native.write_bytes(b'original')
    entered, release, finished = (threading.Event() for _ in range(3))
    app = FastAPI()

    @app.get('/blocked-browser')
    def blocked_browser():
        # The browser waits on actual admitted HTTP work, independently of
        # the urlopen client that verifies the native publication response.
        Path(name + '.browser-request.json').write_text(json.dumps({'pending': True}))
        assert finished.wait(30), 'Native publication did not finish'
        return {'published': True}

    @app.get('/storage-maintenance-status')
    def response():
        entered.set()
        observation = {}
        try:
            assert release.wait(30), 'Native work was not released'
            # Exercise the same fresh read-only connection boundary as the
            # reported maintenance request, plus a real response write.
            reader = store._make_reader()
            try:
                assert reader.conn.execute('SELECT count(*) FROM retained').fetchone()[0] == 0
            finally:
                reader.conn.close()
            assert native.read_bytes() == b'original'
            with store.conn:
                store.conn.execute("INSERT INTO retained VALUES ('published')")
            native.write_bytes(b'published')
            observation['published'] = True
            return {'published': True}
        except BaseException as error:
            observation['error'] = repr(error)
            observation['traceback'] = traceback.format_exc()
            raise
        finally:
            Path(name + '.response.json').write_text(json.dumps(observation))
            finished.set()

    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='info'))
    live = LiveServer(server, sockets=[listener])
    phase_times['server_ready'] = time.monotonic() - owner_started

    def release_after_shutdown():
        while not server.should_exit:
            time.sleep(.01)
        shutdown = time.monotonic()
        # Keep the real former five-second gateway boundary in the four-worker
        # coverage control. Other modes test distinct protocol transitions and
        # release on observed shutdown instead of repeating that elapsed wait.
        # Ordinary teardown still crosses its original ten-second deadline.
        hold = 12.2 if os.environ.get('COMPLETE_BODY') else float(os.environ.get('DRAIN_HOLD', '0'))
        if hold:
            time.sleep(hold)
        Path(name + '.release.json').write_text(json.dumps({
            'shutdown': shutdown, 'released': time.monotonic(), 'hold': hold}))
        release.set()

    def get_response():
        observation = {}
        try:
            with urlopen(f'http://127.0.0.1:{port}/storage-maintenance-status', timeout=30) as result:
                observation = {'status': result.status, 'body': json.load(result)}
        except BaseException as error:
            observation['error'] = repr(error)
        finally:
            Path(name + '.http.json').write_text(json.dumps(observation))

    releaser = threading.Thread(target=release_after_shutdown)
    client = threading.Thread(target=get_response)
    releaser.start()
    client.start()
    try:
        assert entered.wait(5), 'HTTP work did not start'
        phase_times['http_admitted'] = time.monotonic() - owner_started
        Path(name + '.started.json').write_text(json.dumps({'workspace': str(tmp_path)}))
        if os.environ.get('INTERRUPT_SETUP'):
            if page:
                page.evaluate('url => fetch(url)', f'http://127.0.0.1:{port}/blocked-browser')
            else:
                time.sleep(60)
        yield store, page, f'http://127.0.0.1:{port}/blocked-browser'
    finally:
        try:
            drain_started = time.monotonic()
            monitor = request.config.pluginmanager.getplugin('test-session-time-limit')
            Path(name + '.draining.json').write_text(json.dumps({
                'started': time.monotonic(), 'deadline': monitor.deadline if monitor else None,
                'interruption': monitor.interruption if monitor else None}))
            if page and os.environ.get('COMPLETE_BODY'):
                # Cross the deadline inside a real dispatcher call during
                # finalization. The pending stop must let that call finish.
                page.wait_for_timeout(10000)
            live.close()
            client.join()
            releaser.join()
        finally:
            observation = {'drained': finished.is_set(), 'tasks': len(server.server_state.tasks),
                           'native': native.read_bytes().decode(), 'workspace': str(tmp_path),
                           'finalized': time.monotonic(), 'phase_times': phase_times,
                           'admission_to_drain': drain_started - owner_started,
                           'drain_seconds': time.monotonic() - drain_started}
            store.conn.close()
            listener.close()
            Path(name + '.finalized.json').write_text(json.dumps(observation))

def test_interrupted(owner, request):
    owner, page, url = owner
    name = Path(request.node.path).stem
    Path(name + '.body-entered.json').write_text(json.dumps({'entered': True}))
    # Give coverage a real production read in the test's run context before
    # interruption; setup imports alone cannot establish context preservation.
    reader = owner._make_reader()
    try:
        assert reader.conn.execute('SELECT count(*) FROM retained').fetchone()[0] == 0
    finally:
        reader.conn.close()
    if os.environ.get('COMPLETE_BODY'):
        Path(name + '.body-returned.json').write_text(json.dumps({'returned': True}))
        return
    if os.environ.get('DIRECT_INTERRUPT'):
        raise KeyboardInterrupt('Controlled protocol interruption')
    if page:
        if os.environ.get('WAIT_CALLBACK'):
            def nested_request(route):
                Path(name + '.callback-entered.json').write_text(json.dumps({'entered': True}))
                try:
                    response = route.fetch(timeout=0)
                    Path(name + '.callback-response.json').write_text(json.dumps({'status': response.status}))
                    route.fulfill(response=response)
                finally:
                    Path(name + '.callback-finished.json').write_text(json.dumps({'finished': True}))

            page.route(url, nested_request)
            page.evaluate('url => { fetch(url).catch(() => {}); return true; }', url)
            admission_deadline = time.monotonic() + 3
            while not Path(name + '.callback-entered.json').exists():
                assert time.monotonic() < admission_deadline, 'Nested route did not start'
                page.wait_for_timeout(10)
            # The route's sync fetch remains suspended in its own fiber after
            # this ordinary caller has returned. The monitor must still stop
            # Python work here, then let owned HTTP/publication cleanup drain.
            Path(name + '.outside-browser.json').write_text(json.dumps({
                'callback_pending': not Path(name + '.callback-finished.json').exists()}))
            if os.environ.get('ACTIVE_AFTER_CALLBACK'):
                page.wait_for_timeout(60000)
            else:
                time.sleep(60)
        elif os.environ.get('WAIT_EVENT'):
            with page.expect_response(lambda response: response.url == url, timeout=0):
                page.evaluate('url => { fetch(url); return true; }', url)
        else:
            page.evaluate('url => fetch(url)', url)
    else:
        time.sleep(60)
''')
    names = ('one', 'two', 'three', 'four') if monitor == 'coverage' else ('one', 'two')
    for name in names:
        (root / 'tests' / f'test_{name}.py').write_text(
            'from server_probe import owner, test_interrupted\n')
    basetemp = root / 'server-custody-temp'
    options = {'pace': ['--test-runtime-target=.01', '--test-runtime-slack=8'],
               'deadline': ['--test-time-limit=8'], 'interrupt': ['tests/test_one.py'],
               'setup': ['--test-time-limit=8'], 'teardown': ['--test-time-limit=10'],
               'event': ['--test-time-limit=8'],
               'callback': ['--test-time-limit=8'],
               'callback-active': ['--test-time-limit=8'],
               'coverage': ['-n', '4', '-p', 'pytest_cov.plugin', '--cov=yt_playlist',
                            '--cov-context=test', '--cov-report=',
                            '--test-progress=' + str(root / 'progress.jsonl'),
                            '--cov-config=' + str(Path(__file__).resolve().parents[1] / 'pyproject.toml'),
                            '--test-runtime-target=.01', '--test-runtime-slack=15']}[monitor]
    environment = {'interrupt': {'DIRECT_INTERRUPT': '1'}, 'setup': {'INTERRUPT_SETUP': '1'},
                   'teardown': {'COMPLETE_BODY': '1'}, 'event': {'WAIT_EVENT': '1'},
                   'callback': {'WAIT_CALLBACK': '1'},
                   'callback-active': {'WAIT_CALLBACK': '1', 'ACTIVE_AFTER_CALLBACK': '1'}}.get(monitor, {})
    if monitor == 'coverage':
        environment['DRAIN_HOLD'] = '6.2'
    if browser:
        options += ['-p', 'pytest_playwright.pytest_playwright', '-p', 'pytest_base_url.plugin',
                    '--tracing=on', '--output=' + str(root / 'browser-output')]
        environment['USE_BROWSER'] = '1'
    child_started = time.perf_counter()
    try:
        result = run('--basetemp', str(basetemp), *options, **environment)
    finally:
        request.node.user_properties.append(('child_wall_seconds', time.perf_counter() - child_started))
    diagnostic = result.stdout + result.stderr
    request.node.user_properties.append(('child_group_cleanup', json.dumps(result.cleanup_receipt)))
    observations = {path.name: json.loads(path.read_text()) for path in root.glob('test_*.json')}
    request.node.user_properties.append(('worker_observations', json.dumps(observations)))
    request.node.user_properties.append(('child_output', diagnostic))
    assert result.returncode == 2, diagnostic
    if monitor in {'deadline', 'setup', 'teardown', 'callback', 'callback-active'}:
        seconds = 10 if monitor == 'teardown' else 8
        assert f'--test-time-limit={seconds}s expired' in diagnostic
    names = ('one',) if monitor == 'interrupt' else names
    assert len(list(root.glob('*.started.json'))) == len(names), diagnostic
    for name in names:
        if monitor == 'setup':
            assert f'test_{name}.body-entered.json' not in observations, observations
        else:
            assert observations[f'test_{name}.body-entered.json'] == {'entered': True}
        if monitor == 'teardown':
            assert observations[f'test_{name}.body-returned.json'] == {'returned': True}
            draining = observations[f'test_{name}.draining.json']
            assert draining['interruption'] is None, observations
            assert draining['started'] < draining['deadline'], observations
        assert observations[f'test_{name}.response.json'] == {'published': True}, (observations, diagnostic)
        assert observations[f'test_{name}.http.json'] == {'status': 200, 'body': {'published': True}}, diagnostic
        final = observations[f'test_{name}.finalized.json']
        assert (final['drained'], final['tasks'], final['native']) == (True, 0, 'published'), observations
        released = observations[f'test_{name}.release.json']
        expected_hold = 12.2 if monitor == 'teardown' else 6.2 if monitor == 'coverage' else 0
        assert released['hold'] == expected_hold
        assert released['released'] - released['shutdown'] >= expected_hold
        assert final['finalized'] >= released['released']
        if monitor == 'teardown':
            assert final['finalized'] >= draining['deadline'], observations
        assert not Path(final['workspace']).exists()
        if browser and monitor != 'teardown':
            assert observations[f'test_{name}.browser-request.json'] == {'pending': True}
        if monitor in {'callback', 'callback-active'}:
            assert observations[f'test_{name}.outside-browser.json'] == {'callback_pending': True}
            assert observations[f'test_{name}.callback-entered.json'] == {'entered': True}
            assert observations[f'test_{name}.callback-response.json'] == {'status': 200}
            assert observations[f'test_{name}.callback-finished.json'] == {'finished': True}
    assert not basetemp.exists()
    assert 'unable to open database file' not in diagnostic
    assert 'Logging error' not in diagnostic
    assert 'I/O operation on closed file' not in diagnostic
    if browser:
        assert len(list((root / 'browser-output').rglob('trace.zip'))) == len(names), diagnostic
        if monitor not in {'teardown', 'callback'}:
            operation = ('EventInfo.value' if monitor == 'event' else
                         'Page.wait_for_timeout' if monitor == 'callback-active' else 'Page.evaluate')
            assert 'Browser stop: ' + operation in diagnostic, diagnostic
        for marker in ('Task was destroyed', 'exception was never retrieved'):
            assert marker not in diagnostic, diagnostic
    if monitor == 'coverage':
        from coverage import Coverage

        coverage = Coverage(data_file=str(root / '.coverage'))
        coverage.load()
        assert {f'tests/test_{name}.py::test_interrupted|run' for name in names} <= (
            coverage.get_data().measured_contexts())


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
