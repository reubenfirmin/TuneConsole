"""Coverage admission owns real namespace locks, not repeated pytest startups."""
from contextlib import ExitStack, contextmanager
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tomllib
from types import SimpleNamespace

from coverage import Coverage, CoverageData
import pytest

import pytest_coverage_guard as guard
from tests.process_coverage import child_environment


@contextmanager
def early_config(directory, destination, **overrides):
    configuration = directory / 'coverage.ini'
    configuration.write_text(f'[run]\ncore = ctrace\ndata_file = {destination}\n')
    options = SimpleNamespace(cov_source=['subject'], cov_config=str(configuration),
        no_cov=False, cov_append=False, cov_report={'': None},
        fixture_root=str(directory), basetemp=None)
    vars(options).update(overrides)
    with ExitStack() as cleanup:
        yield SimpleNamespace(add_cleanup=cleanup.callback, known_args_namespace=options,
            trace=SimpleNamespace(get=lambda name: lambda *args: None))


def advance(hook):
    with pytest.raises(StopIteration):
        hook.send(None)


def checkpoint(destination, context):
    data = CoverageData(basename=str(destination))
    try:
        data.set_context(context)
        data.add_lines({str(Path(__file__).resolve()): {1}})
        data.write()
    finally:
        data.close()


@pytest.mark.parametrize('spelling,append', [
    ('same', False), ('same', True), ('symlink', False), ('environment', False),
    ('parent', False), ('parent', True), ('child', False), ('child', True)])
def test_early_guard_rejects_overlapping_destinations_before_data_changes(
        tmp_path, monkeypatch, spelling, append):
    destination = tmp_path / 'coverage.sqlite3.focused'
    checkpoint(destination, 'owner-checkpoint')
    checkpoint(destination.with_name(destination.name + '.parallel'), 'owner-parallel')
    snapshots = {path: path.read_bytes() for path in tmp_path.glob(destination.name + '*')}
    target = destination
    if spelling == 'symlink':
        alias = tmp_path / 'alias'
        alias.symlink_to(tmp_path, target_is_directory=True)
        target = alias / destination.name
    elif spelling == 'parent':
        target = tmp_path / 'coverage.sqlite3'
    elif spelling == 'child':
        target = destination.with_name(destination.name + '.more')
    with ExitStack() as owner:
        guard._reserve(SimpleNamespace(add_cleanup=owner.callback), destination)
        with early_config(tmp_path, target, cov_append=append) as config:
            if spelling == 'environment':
                # Let coverage itself resolve the environment override.
                config.known_args_namespace.cov_config = False
                monkeypatch.setenv('COVERAGE_FILE', str(destination))
            with pytest.raises(pytest.UsageError, match='Coverage destination busy') as failure:
                next(guard.pytest_load_initial_conftests(config))
            assert str(destination) in str(failure.value)
            assert f'"pid": {os.getpid()}' in str(failure.value)
            assert '--cov-config' in str(failure.value)
        assert {path: path.read_bytes() for path in snapshots} == snapshots


@pytest.mark.parametrize('suffix', ['.independent', '-independent', 'disabled'])
def test_early_guard_admits_independent_or_disabled_coverage(tmp_path, suffix):
    destination = tmp_path / 'coverage.sqlite3.focused'
    checkpoint(destination, 'owner-checkpoint')
    original = destination.read_bytes()
    with ExitStack() as owner:
        guard._reserve(SimpleNamespace(add_cleanup=owner.callback), destination)
        target = (tmp_path / 'coverage.sqlite3.independent' if suffix == '.independent'
                  else destination.with_name(destination.name + suffix))
        with early_config(tmp_path, target, no_cov=suffix == 'disabled') as config:
            hook = guard.pytest_load_initial_conftests(config)
            next(hook)
            if suffix == 'disabled':
                assert not hasattr(config, '_tuneconsole_coverage_owner')
            else:
                assert config._tuneconsole_coverage_owner['destination'] == str(target)
                checkpoint(target, 'independent')
            advance(hook)
        assert destination.read_bytes() == original


def coverage_worker(destination, release, ready):
    try:
        os.environ['PYTEST_XDIST_WORKER'] = 'gw-native'
        with ExitStack() as cleanup:
            config = SimpleNamespace(add_cleanup=cleanup.callback)
            assert guard._reserve(config, destination) is True
            config.workerinput = {'tuneconsole_coverage_owner': config._tuneconsole_coverage_owner}
            guard.pytest_configure(config)
            ready.send(os.getpid())
            assert release.poll(5) and release.recv() == 'finish'
        # Prove locks are closed before the driver retries admission.
        release.send('closed')
    finally:
        ready.close()
        release.close()


def coverage_owner(destination, worker_releases, release, ready):
    context = multiprocessing.get_context('fork')
    workers = []
    try:
        with ExitStack() as cleanup:
            config = SimpleNamespace(add_cleanup=cleanup.callback)
            assert guard._reserve(config, destination) is False
            checkpoint(destination, 'owner-checkpoint')
            checkpoint(destination.with_name(destination.name + '.parallel'), 'owner-parallel')
            identities = []
            for worker_release in worker_releases:
                receive, send = context.Pipe(duplex=False)
                worker = context.Process(target=coverage_worker,
                    args=(destination, worker_release, send))
                worker.start()
                send.close()
                workers.append(worker)
                try:
                    assert receive.poll(3), 'Coverage worker did not join its coordinator'
                    identities.append(receive.recv())
                finally:
                    receive.close()
            ready.send({'pid': os.getpid(), 'workers': identities})
            assert release.poll(5) and release.recv() == 'finish'
            for worker in workers:
                worker.join(3)
                assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.kill()
            worker.join(3)
        ready.close()
        release.close()


@pytest.mark.parametrize('workers', [0, 2])
@pytest.mark.parametrize('owner_exit', ['complete', 'killed'])
def test_native_coverage_locks_survive_until_owner_and_workers_exit(tmp_path, workers, owner_exit):
    context = multiprocessing.get_context('fork')
    destination = tmp_path / 'coverage.sqlite3.focused'
    release, child_release = context.Pipe()
    worker_releases = [context.Pipe() for _ in range(workers)]
    receive, send = context.Pipe(duplex=False)
    owner = context.Process(target=coverage_owner,
        args=(destination, [child for _, child in worker_releases], child_release, send))
    owner.start()
    send.close()
    child_release.close()
    for _, child in worker_releases:
        child.close()
    try:
        assert receive.poll(3), 'Coverage owner did not reserve its namespace'
        identity = receive.recv()
        snapshots = {path: path.read_bytes() for path in tmp_path.glob(destination.name + '*')}
        assert len(snapshots) == 2
        for target in (destination, tmp_path / 'coverage.sqlite3',
                       destination.with_name(destination.name + '.more')):
            with ExitStack() as cleanup, pytest.raises(pytest.UsageError) as failure:
                guard._reserve(SimpleNamespace(add_cleanup=cleanup.callback), target)
            assert f'"pid": {identity["pid"]}' in str(failure.value)
        if owner_exit == 'killed':
            owner.kill()
            # Forked workers still hold the multiprocessing sentinel's write
            # end. Wait for the killed PID, not that shared lifetime pipe.
            os.waitid(os.P_PID, owner.pid, os.WEXITED | os.WNOWAIT)
            assert owner.exitcode < 0
            for pid in identity['workers']:
                os.kill(pid, 0)
            if workers:
                for target in (destination, tmp_path / 'coverage.sqlite3',
                               destination.with_name(destination.name + '.more')):
                    with ExitStack() as cleanup, pytest.raises(pytest.UsageError):
                        guard._reserve(SimpleNamespace(add_cleanup=cleanup.callback), target)
        assert {path: path.read_bytes() for path in snapshots} == snapshots
        for control, _ in worker_releases:
            control.send('finish')
        if owner_exit == 'complete':
            release.send('finish')
        owner.join(3)
        if owner_exit == 'complete':
            assert owner.exitcode == 0
        for control, _ in worker_releases:
            assert control.poll(3) and control.recv() == 'closed'
        # Stable lock inodes remain. Admission must recover from both normal
        # completion and a killed coordinator without deleting those files.
        with ExitStack() as cleanup:
            assert guard._reserve(SimpleNamespace(add_cleanup=cleanup.callback), destination) is False
    finally:
        if owner.is_alive():
            owner.kill()
        owner.join(3)
        receive.close()
        release.close()
        for control, _ in worker_releases:
            control.close()


def test_worker_early_parse_defers_collector_and_requires_verified_identity(tmp_path, monkeypatch):
    destination = tmp_path / 'coverage.sqlite3'
    plugins = {}
    with early_config(tmp_path, destination) as config:
        identity = {'destination': str(destination), 'pid': os.getppid()}
        def reserve(early, path):
            assert path == destination
            early._tuneconsole_coverage_owner = identity
            early._tuneconsole_coverage_worker = True
            return True
        # Native admission and lifetime are independently exercised above.
        monkeypatch.setattr(guard, '_reserve', reserve)
        config.pluginmanager = SimpleNamespace(register=lambda plugin, name: plugins.setdefault(name, plugin))
        hook = guard.pytest_load_initial_conftests(config)
        next(hook)
        assert config.known_args_namespace.cov_source == []
        advance(hook)
        from pytest_cov.plugin import CovPlugin
        assert isinstance(plugins['_cov'], CovPlugin)
        assert plugins['_cov'].cov_controller is None
        assert config.known_args_namespace.cov_source == ['subject']
        node = SimpleNamespace(config=config, workerinput={})
        guard.pytest_configure_node(node)
        assert node.workerinput == {'tuneconsole_coverage_owner': identity}
        config.workerinput = dict(node.workerinput)
        guard.pytest_configure(config)
        config.workerinput = {}
        with pytest.raises(pytest.UsageError, match='no verified local coordinator'):
            guard.pytest_configure(config)


@pytest.mark.parametrize('ending', ['busy', 'disabled', 'complete', 'collection-failure'])
def test_project_early_guard_runs_before_pytest_cov_can_erase_data(tmp_path, ending):
    repo = Path(__file__).resolve().parents[1]
    addopts = tomllib.loads((repo / 'pyproject.toml').read_text())['tool']['pytest']['ini_options']['addopts']
    (tmp_path / 'pytest.ini').write_text(f'[pytest]\naddopts = {addopts}\npythonpath = {repo} .\n')
    (tmp_path / 'subject.py').write_text('def choose():\n    return 10\n')
    (tmp_path / 'test_subject.py').write_text('import subject\ndef test_choose():\n    assert subject.choose() == 10\n')
    destination = tmp_path / 'coverage.sqlite3'
    checkpoint(destination, 'earlier-owner')
    original = destination.read_bytes()
    config = tmp_path / 'coverage.ini'
    config.write_text(f'[run]\ncore = ctrace\ndata_file = {destination}\n')
    command = [sys.executable, '-m', 'pytest', '-p', 'pytest_cov.plugin',
        '--cov=subject', '--cov-context=test', '--cov-report=', f'--cov-config={config}']
    if ending == 'disabled':
        command.append('--no-cov')
    command.append('missing_test.py' if ending == 'collection-failure' else 'test_subject.py')
    environment = {**child_environment(), 'PYTHONPATH': str(repo), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
    with ExitStack() as cleanup:
        if ending in {'busy', 'disabled'}:
            guard._reserve(SimpleNamespace(add_cleanup=cleanup.callback), destination)
        result = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=5)
        if ending == 'busy':
            assert result.returncode == 4 and 'Coverage destination busy' in result.stderr
        elif ending == 'collection-failure':
            assert result.returncode == 4 and 'not found' in result.stderr
        else:
            assert result.returncode == 0, result.stdout + result.stderr
        if ending in {'busy', 'disabled'}:
            assert destination.read_bytes() == original
    # Admission after collection failure must also release its real locks.
    with ExitStack() as cleanup:
        assert guard._reserve(SimpleNamespace(add_cleanup=cleanup.callback), destination) is False
    if ending == 'complete':
        measured = Coverage(data_file=str(destination), config_file=False)
        measured.load()
        try:
            assert 'test_subject.py::test_choose|run' in measured.get_data().measured_contexts()
            assert 'earlier-owner' not in measured.get_data().measured_contexts()
        finally:
            measured.get_data().close()
