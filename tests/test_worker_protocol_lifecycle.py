"""Own protocol cleanup, HTTP draining and worker coverage at their boundaries."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
from pathlib import Path
import signal
import socket
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
from urllib.request import urlopen

from fastapi import FastAPI
import pytest
import uvicorn
from _pytest.runner import SetupState

from tests.live_server import LiveServer
from yt_playlist.core.store import Store


@pytest.mark.parametrize('phase', ['pace', 'deadline', 'setup', 'teardown', 'interrupt'])
def test_protocol_drains_admitted_http_before_storage_and_capture_close(
        tmp_path, monkeypatch, capsys, request, phase):
    from pytest_time_limit import RuntimeTarget, SessionLimit

    plugin = request.config.pluginmanager.getplugin(str(Path(__file__).resolve().parents[1] / 'conftest.py'))
    assert plugin is not None
    clock = [0.]
    monkeypatch.setattr('pytest_time_limit._monotonic', lambda: clock[0])
    monkeypatch.setattr('pytest_time_limit.coverage_data_locked', lambda: False)
    # Deliver the real monitor callback deterministically, without replacing
    # this containing pytest session's alarm or waiting for its wall clock.
    monkeypatch.setattr(signal, 'setitimer', lambda *args: None)
    monkeypatch.setattr(signal, 'signal', lambda *args: None)
    config = SimpleNamespace(pluginmanager=SimpleNamespace(getplugin=lambda name: (
        monitor if name == 'test-session-time-limit' else None)))
    monitor = RuntimeTarget(config, 1, 1) if phase == 'pace' else SessionLimit(config, 10)
    monitor.deadline = 10
    monitor.previous_handler = object()
    state = SetupState()
    item = SimpleNamespace(config=config, session=SimpleNamespace(_setupstate=state), stash=pytest.Stash())
    monitor.pytest_runtest_protocol(item)
    from _pytest.tmpdir import tmppath_result_key
    assert item.stash[tmppath_result_key] == {}
    workspace = TemporaryDirectory(dir=tmp_path)
    directory = Path(workspace.name)
    store = Store(directory / 'owned.sqlite3')
    # The lifetime contract needs an actual disk-backed Store and a fresh
    # production reader, not migrations or the application's unrelated schema.
    with store.conn:
        store.conn.execute('CREATE TABLE retained(value)')
    native = directory / 'response.evidence'
    native.write_bytes(b'original')
    entered, release, waiting = (threading.Event() for _ in range(3))
    observations = {}
    app = FastAPI()

    @app.get('/storage-maintenance-status')
    def response():
        entered.set()
        assert release.wait(3), 'Native response was not released'
        reader = store._make_reader()
        try:
            assert reader.conn.execute('SELECT count(*) FROM retained').fetchone()[0] == 0
        finally:
            reader.conn.close()
        assert native.read_bytes() == b'original'
        with store.conn:
            store.conn.execute("INSERT INTO retained VALUES ('published')")
        native.write_bytes(b'published')
        print('native response finished before capture closed')
        observations['published'] = True
        return {'published': True}

    server = uvicorn.Server(uvicorn.Config(app, log_level='warning',
        ws='none', loop='asyncio', http='h11', lifespan='off'))
    live = None
    if phase == 'pace':
        monitor.started, monitor.selected = 0., 1
        monitor.state_path = tmp_path / 'runtime-state.json'
        monitor.state_path.write_text('{"stop":false}')

    def complete(iterator):
        with pytest.raises(StopIteration):
            iterator.send(None)

    try:
        with socket.socket() as listener, ThreadPoolExecutor(max_workers=2) as clients:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
            live = LiveServer(server, sockets=[listener])
            original_wait = live._finished.wait

            def wait_for_completion(timeout=None):
                assert timeout is None, 'A timed wait can abandon an admitted response'
                waiting.set()
                if phase == 'teardown':
                    clock[0] = 12.2
                    monitor.expired(signal.SIGALRM, None)
                    assert monitor.interruption and not monitor.delivered
                return original_wait()

            monkeypatch.setattr(live._finished, 'wait', wait_for_completion)

            def fetch():
                with urlopen(f'http://127.0.0.1:{port}/storage-maintenance-status', timeout=3) as result:
                    return result.status, json.load(result)

            def release_after_shutdown():
                try:
                    assert waiting.wait(3), 'Protocol did not drain its live server'
                    assert server.should_exit
                    assert not observations
                    assert native.read_bytes() == b'original'
                    assert store.conn.execute('SELECT count(*) FROM retained').fetchone()[0] == 0
                    if phase != 'teardown':
                        assert monitor.previous_handler is None, 'Monitor must stop before draining fixtures'
                finally:
                    release.set()

            response_future = clients.submit(fetch)
            assert entered.wait(3)
            releaser = clients.submit(release_after_shutdown)
            state.stack[object()] = ([workspace.cleanup, store.conn.close, live.close], None)
            protocol = plugin.pytest_runtest_protocol(item, None)
            next(protocol)
            body_entered = False
            if phase != 'setup':
                body_entered = True
                reader = store._make_reader()
                try:
                    assert reader.conn.execute('SELECT count(*) FROM retained').fetchone()[0] == 0
                finally:
                    reader.conn.close()
            with pytest.raises(KeyboardInterrupt) as stopped:
                if phase == 'teardown':
                    # Pytest has already popped this finalizer when its alarm
                    # fires. The monitor must defer until native work finishes.
                    teardown = monitor.pytest_runtest_teardown(item, None)
                    next(teardown)
                    state.teardown_exact(None)
                    complete(teardown)
                    monitor.pytest_runtest_makereport(item, None)
                elif phase == 'interrupt':
                    raise KeyboardInterrupt('Controlled protocol interruption')
                else:
                    # Setup and call interrupts traverse the same outer
                    # protocol guard, including partially acquired fixtures.
                    clock[0] = 12.2
                    monitor.expired(signal.SIGALRM, None)
            with pytest.raises(KeyboardInterrupt) as propagated:
                protocol.throw(stopped.value)
            assert propagated.value is stopped.value
            releaser.result(timeout=3)
            assert response_future.result(timeout=3) == (200, {'published': True})
            assert observations == {'published': True}
            assert body_entered is (phase != 'setup')
            assert not live.thread.is_alive() and not server.server_state.tasks
            assert not state.stack and not directory.exists()
            assert 'native response finished before capture closed' in capsys.readouterr().out
            if phase == 'pace':
                assert json.loads(monitor.state_path.read_text())['stop'] is True
    finally:
        release.set()
        if live is not None:
            # The deliberate alarm belongs to the first teardown only.
            monkeypatch.setattr(live._finished, 'wait', lambda timeout=None: live.thread.join(timeout))
            live.close()
        store.conn.close()
        workspace.cleanup()


def test_coordinator_drains_four_native_worker_channels_before_merging_coverage(tmp_path, monkeypatch, request):
    import execnet
    from coverage import Coverage
    from pytest_cov.engine import DistMaster
    from pytest_progress import InterruptedCoverage

    repo = Path(__file__).resolve().parents[1]
    plugin = request.config.pluginmanager.getplugin(str(repo / 'conftest.py'))
    monkeypatch.setenv('COVERAGE_FILE', str(tmp_path / '.coverage'))
    options = SimpleNamespace(cov_source=['local_test_workers'], cov_report={},
        cov_config=False, cov_append=False, cov_branch=False, cov_precision=0)
    controller = DistMaster(options, None, None)
    controller.topdir = str(tmp_path)
    controller.start()
    controller.started = True
    plugins = {'_cov': SimpleNamespace(cov_controller=controller)}
    config = SimpleNamespace(pluginmanager=SimpleNamespace(getplugin=plugins.get))
    session = SimpleNamespace(config=config)
    with ExitStack() as resources:
        original_coverage = controller.cov
        resources.callback(lambda: original_coverage.get_data().close())
        resources.callback(lambda: controller.cov.get_data().close())
        resources.callback(original_coverage.stop)
        group = execnet.Group()
        resources.callback(group.terminate)
        channels = []
        shutdowns = set()
        for i in range(4):
            gateway = group.makegateway('popen')
            channel = gateway.remote_exec('''
import json, sqlite3, sys
from pathlib import Path
from types import SimpleNamespace
from pytest_cov.engine import DistWorker
from pytest_progress import InterruptedCoverage

repo, directory, index, options = channel.receive()
sys.path.insert(0, repo)
import local_test_workers
directory = Path(directory)
config = SimpleNamespace(workerinput={
    'cov_master_host': __import__('socket').gethostname(),
    'cov_master_topdir': str(directory)}, workeroutput={})
controller = DistWorker(SimpleNamespace(**options), config, 'gw' + str(index))
controller.topdir = str(directory)
controller.start()
config.pluginmanager = SimpleNamespace(getplugin=lambda name: (
    SimpleNamespace(cov_controller=controller) if name == '_cov' else None))
controller.cov.switch_context('tests/test_worker_' + str(index) + '.py::test_interrupted|run')
assert 0 <= local_test_workers.default_workers() <= 4
connection = sqlite3.connect(directory / ('worker-' + str(index) + '.sqlite3'))
connection.execute('CREATE TABLE retained(value)')
connection.commit()
channel.send('admitted')
assert channel.receive() == 'shutdown'
connection.execute("INSERT INTO retained VALUES ('published')")
connection.commit()
connection.close()
hook = InterruptedCoverage(config).pytest_sessionfinish(SimpleNamespace(config=config), 2)
next(hook)
try:
    hook.send(None)
except StopIteration:
    pass
assert 'cov_worker_node_id' in config.workeroutput
(directory / ('worker-' + str(index) + '.finished')).write_text('published')
''')
            channel.send((str(repo), str(tmp_path), i, vars(options)))
            channels.append(channel)

            def shutdown(index=i, channel=channel):
                shutdowns.add(index)
                channel.send('shutdown')

            def waitclose(timeout=None, channel=channel):
                # Make the former five-second gateway deadline fail immediately
                # instead of sleeping beyond it in every run.
                assert timeout is None, 'Worker finalizers must finish before gateway termination'
                assert shutdowns == set(range(4)), 'Stop all workers before waiting for any one'
                channel.waitclose()

            gateway.node = SimpleNamespace(shutdown=shutdown,
                channel=SimpleNamespace(waitclose=waitclose, RemoteError=channel.RemoteError))
        assert [channel.receive(timeout=5) for channel in channels] == ['admitted'] * 4
        plugins['dsession'] = SimpleNamespace(nodemanager=SimpleNamespace(group=group))
        finish = plugin.pytest_sessionfinish(session)
        next(finish)
        # The actual coordinator hook drains all four native execnet channels
        # before yielding to pytest-cov's merge/capture teardown.
        assert all((tmp_path / f'worker-{i}.finished').read_text() == 'published' for i in range(4))
        coverage_finish = InterruptedCoverage(config).pytest_sessionfinish(session, 2)
        next(coverage_finish)
        with pytest.raises(StopIteration):
            coverage_finish.send(None)
        with pytest.raises(StopIteration):
            finish.send(None)
    coverage = Coverage(data_file=str(tmp_path / '.coverage'), config_file=False)
    coverage.load()
    try:
        assert {f'tests/test_worker_{i}.py::test_interrupted|run' for i in range(4)} <= (
            coverage.get_data().measured_contexts())
    finally:
        coverage.get_data().close()
