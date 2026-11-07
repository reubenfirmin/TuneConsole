"""Validate private child inputs and supervision without nested test suites."""
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
from types import SimpleNamespace

from coverage import Coverage
import pytest

from tests import browser_deadline, process_coverage


def execute_subject(path):
    exec(compile(Path(path).read_text(), str(path), 'exec'), {})


@pytest.fixture
def parent_capture(tmp_path):
    subject = tmp_path / 'subject.py'
    subject.write_text('value = 10\nassert value == 10\n')
    parent = Coverage(config_file=False, data_file=str(tmp_path / 'parent.sqlite3'), source=[str(tmp_path)])
    parent.set_option('run:core', 'ctrace')
    parent.start()
    parent.switch_context('outer-sentinel')
    execute_subject(subject)
    parent.switch_context('test_child|run')
    parent.save()
    capture = process_coverage.ChildCoverage(parent, 'test_child', tmp_path, tmp_path / 'report')
    try:
        yield parent, capture, subject
    finally:
        parent.stop()
        parent.get_data().close()


def finalized_child(specification, subject, ending):
    inherited = Coverage.current()
    if inherited:
        inherited.stop()
    collector = Coverage(config_file=specification['config'], data_suffix=True)
    collector.start()
    collector.switch_context(('different-test' if ending == 'wrong-node' else specification['node']) + '|run')
    execute_subject(subject)
    if ending == 'missing':
        collector.stop()
        collector.get_data().close()
        return
    process_coverage.finalize_coverage(collector, specification['marker'], specification['node'], reason='probe')
    data = Path(collector.get_data().data_filename())
    collector.get_data().close()
    if ending == 'corrupt':
        data.write_bytes(b'not a coverage database')
    elif ending == 'escaped':
        marker = Path(specification['marker'])
        receipt = json.loads(marker.read_text())
        receipt['data_files'] = [str(Path(subject).parent / 'parent.sqlite3')]
        marker.write_text(json.dumps(receipt))


@pytest.mark.parametrize('ending', ['complete', 'incomplete', 'missing', 'corrupt', 'wrong-node', 'escaped'])
def test_child_union_validates_native_finalized_inputs_before_parent_update(parent_capture, ending):
    parent, capture, subject = parent_capture
    destination = Path(parent.get_option('run:data_file'))
    original = destination.read_bytes()
    process = multiprocessing.get_context('fork').Process(target=finalized_child,
        args=(capture.specification(), subject, ending))
    process.start()
    try:
        process.join(3)
        assert process.exitcode == 0
        invalid = ending in {'missing', 'corrupt', 'wrong-node', 'escaped'}
        expected = {'missing': (AssertionError, 'no finalized coverage input'),
                    'corrupt': (sqlite3.DatabaseError, None),
                    'wrong-node': (AssertionError, 'different test node'),
                    'escaped': (AssertionError, 'escaped its private namespace')}
        if invalid:
            error, message = expected[ending]
            with pytest.raises(error, match=message):
                capture.finish(process, completed=False)
            assert destination.read_bytes() == original
        else:
            capture.finish(process, completed=ending == 'complete')
        receipt = json.loads((capture.report / 'receipt.json').read_text())
        assert receipt['coverage_valid'] is (not invalid)
        assert receipt['coverage_complete'] is (ending == 'complete')
        assert 'outer-sentinel' in parent.get_data().measured_contexts()
        if not invalid:
            assert receipt['run_lines'] == 2
            assert 'test_child|run' in parent.get_data().measured_contexts()
            assert (capture.report / 'coverage.sqlite3').is_file()
    finally:
        if process.is_alive():
            process.kill()
            process.join(3)
        process.close()


def spawned_subject(connection, subject):
    execute_subject(subject)
    connection.send('ready')
    assert connection.recv() == 'work'
    execute_subject(subject)
    connection.send('done')
    assert connection.recv() == 'finish'
    execute_subject(subject)
    connection.close()


def terminated_child(specification, subject, ready):
    inherited = Coverage.current()
    if inherited:
        inherited.stop()
    collector = Coverage(config_file=specification['config'], data_suffix=True)
    collector.start()
    collector.switch_context(specification['node'] + '|run')
    execute_subject(subject)
    process_coverage.sigterm_snapshot(lambda: collector, specification['marker'], specification['node'])
    ready.send('recording')
    signal.pause()


def test_terminated_child_finalizes_real_partial_coverage_before_exit(parent_capture):
    parent, capture, subject = parent_capture
    context = multiprocessing.get_context('fork')
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=terminated_child, args=(capture.specification(), subject, send))
    process.start()
    send.close()
    try:
        assert receive.poll(3) and receive.recv() == 'recording'
        process.terminate()
        process.join(3)
        assert process.exitcode == -signal.SIGTERM
        capture.finish(process, completed=False)
        receipt = json.loads((capture.report / 'receipt.json').read_text())
        assert receipt['coverage_valid'] is True and receipt['coverage_complete'] is False
        assert receipt['finalization']['reason'] == 'sigterm'
        assert receipt['run_lines'] == 2
        assert 'outer-sentinel' in parent.get_data().measured_contexts()
    finally:
        if process.is_alive():
            process.kill()
            process.join(3)
        process.close()
        receive.close()


def test_spawned_capture_tracks_actual_phases_and_rejects_running_child(parent_capture, monkeypatch):
    parent, capture, subject = parent_capture
    context = multiprocessing.get_context('spawn')
    phase, child_phase = context.Pipe()
    work, child_work = context.Pipe()
    monkeypatch.setenv('COVERAGE_FILE', parent.get_option('run:data_file'))
    monkeypatch.setenv('COVERAGE_FORCE_CONFIG', 'inherited-invalid-configuration')
    process = context.Process(target=process_coverage.run_spawned,
        args=(capture.specification(), child_phase, spawned_subject, (child_work, subject)))
    process.start()
    child_phase.close()
    child_work.close()
    try:
        assert work.poll(3) and work.recv() == 'ready'
        with pytest.raises(AssertionError, match='running child'):
            capture.finish(process, completed=False)
        process_coverage.switch_spawned_phase(phase, 'run')
        work.send('work')
        assert work.poll(3) and work.recv() == 'done'
        process_coverage.switch_spawned_phase(phase, 'teardown')
        work.send('finish')
        process.join(3)
        assert process.exitcode == 0
        capture.finish(process, completed=True)
        assert {'outer-sentinel', *(f'test_child|{phase}' for phase in ('setup', 'run', 'teardown'))} <= (
            parent.get_data().measured_contexts())
    finally:
        if process.is_alive():
            process.kill()
            process.join(3)
        process.close()
        phase.close()
        work.close()


def test_optional_capture_requires_current_guarded_collector(parent_capture, tmp_path):
    parent, capture, _subject = parent_capture
    plugins = {}
    request = SimpleNamespace(node=SimpleNamespace(nodeid='test_optional'),
        config=SimpleNamespace(pluginmanager=SimpleNamespace(getplugin=plugins.get)))
    assert process_coverage.ChildCoverage.optional(request, tmp_path, tmp_path / 'report') is None
    controller = SimpleNamespace(started=False, cov=parent)
    plugins['_cov'] = SimpleNamespace(cov_controller=controller)
    assert process_coverage.ChildCoverage.optional(request, tmp_path, tmp_path / 'report') is None
    controller.started = True
    with pytest.raises(AssertionError, match='current guarded pytest-cov owner'):
        process_coverage.ChildCoverage.optional(request, tmp_path, tmp_path / 'report')
    request.config._tuneconsole_coverage_owner = {'destination': str(Path(parent.get_option('run:data_file')).resolve())}
    created = process_coverage.ChildCoverage.optional(request, tmp_path, tmp_path / 'report')
    assert created.parent is parent and created.node == 'test_optional'
    assert created.directory != capture.directory


@pytest.mark.parametrize('ending', ['complete', 'timeout', 'kill', 'disabled'])
def test_supervisor_cleans_owned_group_before_merging_or_removing_workspace(
        tmp_path, monkeypatch, ending):
    events = []
    captured = {}
    class Worker:
        pid = 43210
        returncode = None
        def wait(self, timeout):
            events.append(('wait', timeout))
            if self.returncode is None and ending in {'timeout', 'kill'}:
                if timeout == 12 or ending == 'kill':
                    raise subprocess.TimeoutExpired('controlled worker', timeout)
            self.returncode = 0 if ending in {'complete', 'disabled'} else -signal.SIGTERM
            return self.returncode
    worker = Worker()
    def launch(command, **options):
        captured.update(command=command, options=options)
        assert Path(options['env']['TMPDIR']).is_dir()
        assert options['start_new_session'] is True
        return worker
    def signal_group(pid, sig):
        assert pid == worker.pid
        events.append(('signal', sig))
        if sig == signal.SIGKILL and worker.returncode is None:
            worker.returncode = -signal.SIGKILL
    def finish(process, *, completed):
        assert process.returncode is not None
        assert ('signal', signal.SIGKILL) in events
        assert Path(captured['options']['env']['TMPDIR']).exists()
        events.append(('finish', completed))
    capture = SimpleNamespace(marker=tmp_path / 'marker',
        pytest_arguments=lambda: ['--cov-config=private.ini'], finish=finish)
    monkeypatch.setattr(browser_deadline.ChildCoverage, 'optional',
        lambda *args: None if ending == 'disabled' else capture)
    monkeypatch.setattr(browser_deadline.subprocess, 'Popen', launch)
    monkeypatch.setattr(browser_deadline.os, 'killpg', signal_group)
    monkeypatch.setenv('PYTEST_ADDOPTS', '--test-lock-wait=99 --lf --cov=wrong --invalid-option')
    monkeypatch.setenv('PYTEST_XDIST_WORKER', 'inherited-worker')
    monkeypatch.setenv('TUNECONSOLE_COVERAGE_OWNER', 'inherited-owner')
    monkeypatch.setenv('COVERAGE_FILE', 'parent.sqlite3')
    request = SimpleNamespace(node=SimpleNamespace(nodeid='tests/test_workflow.py::test_journey'),
        config=SimpleNamespace(rootpath=tmp_path))
    report = tmp_path / 'report'
    if ending in {'timeout', 'kill'}:
        with pytest.raises(browser_deadline.JourneyTimeout, match='exceeded 12s'):
            browser_deadline.supervise_journey(request, 'chromium', report, seconds=12)
    else:
        assert browser_deadline.supervise_journey(request, 'chromium', report, seconds=12) is False
    environment = captured['options']['env']
    assert environment['PYTEST_ADDOPTS'] == ''
    assert not {'PYTEST_XDIST_WORKER', 'COVERAGE_FILE', 'TUNECONSOLE_COVERAGE_OWNER'} & environment.keys()
    assert environment[browser_deadline.WORKER] == request.node.nodeid
    assert not Path(environment['TMPDIR']).exists()
    if ending == 'disabled':
        assert not any(event[0] == 'finish' for event in events)
    else:
        assert ('finish', ending == 'complete') in events
    assert ('--no-cov' in captured['command']) is (ending == 'disabled')
    assert (report / 'worker-timing.json').is_file()


def test_supervisor_worker_does_not_launch_another_process(tmp_path, monkeypatch):
    node = 'tests/test_workflow.py::test_journey'
    monkeypatch.setenv(browser_deadline.WORKER, node)
    assert browser_deadline.supervise_journey(SimpleNamespace(node=SimpleNamespace(nodeid=node)),
        'chromium', tmp_path / 'report') is True
    assert not (tmp_path / 'report').exists()
