"""Optional coverage for test-owned children, merged only after they exit."""
from configparser import ConfigParser
from contextlib import ExitStack, closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import threading
from types import SimpleNamespace
from uuid import uuid4


def child_environment():
    environment = dict(os.environ)
    # A focused child owns neither the suite/resume options nor its parent's
    # coverage destination. Its explicit command supplies all test options.
    for name in tuple(environment):
        if name.startswith(('COV_CORE_', 'COVERAGE_', 'PYTEST_XDIST_', 'TUNECONSOLE_CHILD_COVERAGE_')) or name in {
                'TUNECONSOLE_COVERAGE_OWNER', 'PYTEST_PLUGINS'}:
            environment.pop(name)
    environment['PYTEST_ADDOPTS'] = ''
    return environment


class ChildCoverage:
    @classmethod
    def optional(cls, request, workspace, report):
        plugin = request.config.pluginmanager.getplugin('_cov')
        controller = getattr(plugin, 'cov_controller', None)
        if controller is None or not controller.started:
            return None
        owner = getattr(request.config, '_tuneconsole_coverage_owner', None)
        if not owner or Path(owner['destination']) != Path(controller.cov.get_option('run:data_file')).resolve():
            raise AssertionError('Child coverage requires the current guarded pytest-cov owner')
        return cls(controller.cov, request.node.nodeid, workspace, report)

    def __init__(self, parent, node, workspace, report):
        self.parent, self.node = parent, node
        self.directory = Path(workspace) / ('coverage-' + uuid4().hex)
        self.directory.mkdir()
        self.report = Path(report) / self.directory.name
        self.data = self.directory / 'child.sqlite3'
        self.config = self.directory / 'coverage.ini'
        self.marker = self.directory / 'finalized.json'
        self.context = parent.get_option('run:context')
        # Copy collection semantics, including effective CLI source/branch
        # overrides. Deliberately exclude automatic descendant patches and
        # shared debug/output paths: each supported child has an explicit owner.
        options = ('branch', 'source', 'source_pkgs', 'source_dirs', 'include', 'omit',
                   'relative_files', 'cover_pylib', 'timid', 'concurrency', 'plugins',
                   'disable_warnings', 'context')
        parser = ConfigParser(interpolation=None)
        parser['run'] = {'data_file': str(self.data).replace('$', '$$'), 'core': 'ctrace',
                         'parallel': 'true', 'sigterm': 'true'}

        def value(item):
            if isinstance(item, (list, tuple)):
                item = '\n' + '\n'.join(item)
            return str(item).replace('$', '$$')

        for option in options:
            item = parent.get_option('run:' + option)
            if option == 'concurrency' and item:
                item = [name for name in item if name != 'multiprocessing']
            if item is not None:
                parser['run'][option] = value(item)
        for plugin in parent.get_option('run:plugins') or ():
            parser[plugin] = {key: value(item) for key, item in parent.config.plugin_options.get(plugin, {}).items()}
        parser['paths'] = {key: value(item) for key, item in parent.get_option('paths').items()}
        with self.config.open('w') as stream:
            parser.write(stream)

    def pytest_arguments(self):
        return ['--cov', '--cov-context=test', '--cov-report=', '--cov-fail-under=0',
                '--cov-config=' + str(self.config)]

    def specification(self):
        return {'config': str(self.config), 'data': str(self.data), 'node': self.node,
                'marker': str(self.marker)}

    def finish(self, process, *, completed):
        """Validate a private stopped-child union before touching parent data."""
        from coverage import CoverageData

        exit_code = process.poll() if hasattr(process, 'poll') else process.exitcode
        assert exit_code is not None, 'Cannot merge coverage from a running child'
        self.report.mkdir(parents=True)
        receipt = {'nodeid': self.node, 'exit_code': exit_code,
                   'workflow_completed': completed, 'coverage_valid': False,
                   'coverage_complete': False, 'inputs': []}
        combined = CoverageData(basename=str(self.directory / 'union.sqlite3'))
        try:
            assert self.marker.is_file(), 'Child produced no finalized coverage input'
            finalized = json.loads(self.marker.read_text())
            assert finalized['pid'] == process.pid and finalized['nodeid'] == self.node, 'Wrong child finalization owner'
            paths = [Path(name) for name in finalized['data_files']]
            assert paths and len(paths) == len(set(paths)), 'Invalid finalized coverage inputs'
            for path in paths:
                assert (path.resolve().parent == self.directory.resolve() and path.is_file()
                        and (path.name == self.data.name or path.name.startswith(self.data.name + '.'))), 'Child input escaped its private namespace'
            receipt['finalization'] = finalized
            for index, path in enumerate(paths):
                # Read-only source inspection and backup avoid initializing or
                # repairing an input that should already have been finalized.
                snapshot = self.directory / f'input-{index}.sqlite3'
                with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as source:
                    assert source.execute('PRAGMA integrity_check(10)').fetchall() == [('ok',)]
                    assert not source.execute('PRAGMA foreign_key_check').fetchall()
                    with closing(sqlite3.connect(snapshot)) as target:
                        source.backup(target)
                data = CoverageData(basename=str(snapshot))
                try:
                    data.read()
                    assert data.has_arcs() == self.parent.get_option('run:branch'), 'Child coverage mode changed'
                    combined.update(data)
                finally:
                    data.close()
                receipt['inputs'].append({'name': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
            prefix = (self.context + '|' if self.context else '') + self.node + '|'
            contexts = combined.measured_contexts()
            assert not any(context.endswith(('|setup', '|run', '|teardown'))
                           and context not in {prefix + phase for phase in ('setup', 'run', 'teardown')}
                           for context in contexts), 'Child collected a different test node'
            combined.set_query_context(prefix + 'run')
            application = Path(self.node.split('::', 1)[0]).name in {
                'test_road_trip_browser.py', 'test_recap_browser.py'}
            lines = sum(len(combined.lines(name) or ()) for name in combined.measured_files()
                        if not application or 'yt_playlist' in Path(name).parts)
            assert lines, 'Child workflow has zero production lines in its original run context'
            combined.set_query_contexts(None)
            combined.write()
            # This updates the active collector; loading/replacing its database
            # would discard outer work not yet flushed by pytest-cov.
            self.parent.get_data().update(combined)
            self.parent.save()
            receipt.update(coverage_valid=True, coverage_complete=completed,
                           run_lines=lines, contexts=sorted(contexts),
                           source_scope='application' if application else 'configured parent source')
        except Exception as error:
            receipt['gap'] = f'{type(error).__name__}: {error}'
            raise
        finally:
            combined.close()
            union = self.directory / 'union.sqlite3'
            if union.exists():
                shutil.copyfile(union, self.report / 'coverage.sqlite3')
            (self.report / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')


@contextmanager
def record_spawned(specification, control):
    """Measure an explicitly spawned server without changing its lifetime."""
    from coverage import Coverage
    from pytest_coverage_guard import _reserve

    # Spawn inherits environment rather than the supervisor's cleaned Popen
    # environment. In particular COVERAGE_FORCE_CONFIG must not redirect this
    # collector after its private destination has been reserved.
    environment = child_environment()
    for name in tuple(os.environ):
        if name not in environment:
            os.environ.pop(name)
    os.environ['PYTEST_ADDOPTS'] = ''
    with ExitStack() as cleanup:
        owner = SimpleNamespace(add_cleanup=cleanup.callback)
        _reserve(owner, Path(specification['data']).resolve())
        coverage = Coverage(config_file=specification['config'],
                            data_file=specification['data'], data_suffix=True)
        coverage.set_option('run:core', 'ctrace')
        coverage.start()
        coverage.switch_context(specification['node'] + '|setup')
        restore_signal = sigterm_snapshot(lambda: coverage, specification['marker'], specification['node'])
        stopping = threading.Event()

        def phases():
            try:
                while not stopping.is_set():
                    if control.poll(.1):
                        phase = control.recv()
                        assert phase in {'run', 'teardown'}
                        coverage.switch_context(specification['node'] + '|' + phase)
                        control.send(phase)
            except (EOFError, OSError):
                pass

        listener = threading.Thread(target=phases, name='child-coverage-context', daemon=True)
        listener.start()
        try:
            yield
        finally:
            stopping.set()
            listener.join(timeout=1)
            control.close()
            try:
                finalize_coverage(coverage, specification['marker'], specification['node'], reason='target-return')
            finally:
                coverage.get_data().close()
                restore_signal()


def run_spawned(specification, control, target, args):
    with record_spawned(specification, control):
        target(*args)


def switch_spawned_phase(control, phase):
    control.send(phase)
    assert control.poll(5), 'Spawned coverage context did not acknowledge its phase'
    assert control.recv() == phase


class SpawnedCoveragePhases:
    """Switch the server at actual pytest phase boundaries, after all setup."""
    def __init__(self, node, process, control):
        self.node, self.process, self.control = node, process, control

    def pytest_runtest_call(self, item):
        if item is self.node:
            assert self.process.is_alive(), 'Spawned server exited before the test call'
            switch_spawned_phase(self.control, 'run')

    def pytest_runtest_teardown(self, item):
        if item is self.node and self.process.is_alive():
            switch_spawned_phase(self.control, 'teardown')


def finalize_coverage(coverage, marker, node, *, reason):
    """Publish only after the collector has stopped and its save has returned."""
    coverage.stop()
    coverage.save()
    marker = Path(marker)
    pending = marker.with_suffix('.pending')
    pending.write_text(json.dumps({'pid': os.getpid(), 'nodeid': node, 'reason': reason,
        'data_files': [coverage.get_data().data_filename()]}) + '\n')
    pending.replace(marker)


def sigterm_snapshot(collector, marker, node):
    """Snapshot before the existing TERM handler ends this owned process."""
    previous = signal.getsignal(signal.SIGTERM)

    def snapshot(signum, frame):
        try:
            finalize_coverage(collector(), marker, node, reason='sigterm')
        except BaseException as error:
            # This is termination, not a Python exception through Playwright's
            # synchronous transport. The parent rejects an absent receipt.
            try:
                os.write(2, f'Child coverage snapshot failed: {error!r}\n'.encode())
            except OSError:
                pass
        try:
            signal.signal(signal.SIGTERM, previous)
            if callable(previous):
                previous(signum, frame)
            elif previous != signal.SIG_IGN:
                os.kill(os.getpid(), signum)
        except BaseException:
            # A failed prior handler must not escape into that transport either.
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    signal.signal(signal.SIGTERM, snapshot)
    return lambda: signal.signal(signal.SIGTERM, previous)
