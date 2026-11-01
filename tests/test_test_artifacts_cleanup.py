"""Deletion is confined to explicit disposable workspaces with no active owner."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/test-artifacts-cleanup.py'
SPEC = importlib.util.spec_from_file_location('test_artifacts_cleanup_script', SCRIPT)
cleanup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cleanup
SPEC.loader.exec_module(cleanup)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup, 'REPOSITORY', tmp_path)
    target = tmp_path / '.pytest_cache' / 'disposable'
    target.mkdir(parents=True)
    owner = cleanup.Process(99999999, 'stopped-owner', (), ())
    cleanup.register(target, owner.pid, [owner])
    (target / 'state.db').write_bytes(b'disposable fixture')
    return target


def test_preview_preserves_files_and_delete_removes_only_explicit_target(workspace, monkeypatch, capsys):
    monkeypatch.setattr(cleanup, 'processes', lambda: [])
    evidence = workspace.parent / 'acceptance.xml'
    evidence.write_text('keep this report')
    assert cleanup.main([str(workspace)]) == 0
    assert 'WOULD DELETE' in capsys.readouterr().out
    assert (workspace / 'state.db').exists()
    assert cleanup.main(['--delete', str(workspace)]) == 0
    assert not workspace.exists()
    assert evidence.read_text() == 'keep this report'


def test_live_owner_and_pid_reuse_are_distinguished(workspace):
    saved = cleanup.receipt(workspace)
    assert 'still running' in cleanup.blocker(workspace, [
        cleanup.Process(saved['pid'], saved['start'], (), ())])
    assert cleanup.blocker(workspace, [cleanup.Process(saved['pid'], 'new-start', (), ())]) is None


@pytest.mark.parametrize('reference', ['fd', 'cwd', 'mapping', 'command'])
def test_active_worker_blocks_cleanup_after_coordinator_exit(workspace, reference):
    path = str(workspace / 'nested' / 'state.db')
    args = ('python', '-m', 'pytest', '--basetemp=' + str(workspace)) if reference == 'command' else ('python',)
    refs = () if reference == 'command' else (str(workspace) if reference == 'cwd' else path,)
    reason = cleanup.blocker(workspace, [cleanup.Process(1234, 'worker', args, refs)])
    assert reason and '1234' in reason


def test_real_process_holding_workspace_file_is_detected(workspace):
    child = subprocess.Popen([sys.executable, '-c',
        'import sys; f=open(sys.argv[1],"rb"); print("ready",flush=True); sys.stdin.readline()',
        str(workspace / 'state.db')], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline() == 'ready\n'
        observed = [process for process in cleanup.processes() if process.pid == child.pid]
        assert observed and str(child.pid) in cleanup.blocker(workspace, observed)
    finally:
        child.communicate('\n', timeout=10)


def test_unknown_process_visibility_refuses_deletion(workspace, monkeypatch):
    monkeypatch.setattr(cleanup, 'processes', lambda: [cleanup.Process(1234, '', (), (), unreadable=True)])
    assert cleanup.main(['--delete', str(workspace)]) == 1
    assert workspace.exists()


def test_unregistered_legacy_workspace_waits_for_other_tests(workspace):
    (workspace / cleanup.MARKER).unlink()
    reason = cleanup.blocker(workspace, [cleanup.Process(1234, 'other', ('uv', 'run', 'pytest', 'tests/core'), ())])
    assert 'unregistered' in reason


def test_cleanup_shell_arguments_are_not_mistaken_for_a_pytest_job():
    shell = cleanup.Process(1234, 'shell', ('bash', '-c',
        './scripts/test-artifacts-cleanup.py --delete /tmp/pytest-of-user/pytest-12'), ())
    assert not cleanup.test_job(shell)
    assert cleanup.test_job(cleanup.Process(2345, 'test', ('python', '-m', 'pytest', 'tests/core'), ()))


def test_directory_replaced_during_inspection_survives(workspace, monkeypatch):
    previous = workspace.with_name('original')
    def replaced():
        workspace.rename(previous)
        workspace.mkdir()
        (workspace / 'unrelated').write_text('keep')
        return []
    monkeypatch.setattr(cleanup, 'processes', replaced)
    assert cleanup.main(['--delete', str(workspace)]) == 1
    assert (workspace / 'unrelated').read_text() == 'keep'
    assert (previous / 'state.db').exists()


def test_symlink_target_is_rejected_but_link_inside_workspace_does_not_delete_destination(workspace, monkeypatch):
    monkeypatch.setattr(cleanup, 'processes', lambda: [])
    outside = workspace.parent / 'retained'
    outside.mkdir()
    (outside / 'report.xml').write_text('keep')
    linked = workspace.parent / 'linked'
    linked.symlink_to(workspace, target_is_directory=True)
    assert cleanup.main(['--delete', str(linked)]) == 1
    assert workspace.exists()
    (workspace / 'outside').symlink_to(outside, target_is_directory=True)
    assert cleanup.main(['--delete', str(workspace)]) == 0
    assert (outside / 'report.xml').read_text() == 'keep'


def test_marker_tampering_cannot_authorize_an_unrelated_directory(workspace, monkeypatch):
    marker = workspace / cleanup.MARKER
    saved = json.loads(marker.read_text())
    saved['inode'] += 1
    marker.write_text(json.dumps(saved))
    monkeypatch.setattr(cleanup, 'processes', lambda: [])
    assert cleanup.main(['--delete', str(workspace)]) == 1
    assert workspace.exists()


def test_shared_parents_suite_locks_and_review_reports_are_protected(workspace):
    for target in ('/tmp', cleanup.REPOSITORY / '.pytest_cache', '/tmp/tuneconsole-tests-1000.lock',
                   '/tmp/tuneconsole-served', '/tmp/tuneconsole-review687-20261101'):
        with pytest.raises((ValueError, FileNotFoundError)):
            cleanup.validate_path(target)


def test_registration_requires_real_owner_and_cannot_overwrite_receipt(workspace):
    with pytest.raises(ValueError, match='running'):
        cleanup.register(workspace, 1234, [])
    with pytest.raises(FileExistsError):
        cleanup.register(workspace, 1234, [cleanup.Process(1234, 'new-owner', (), ())])


def test_new_registration_is_limited_to_disposable_namespaces(workspace):
    target = workspace.parent / 'new-experiment'
    target.mkdir()
    with pytest.raises(ValueError, match='not a registered'):
        cleanup.validate_path(target)
    assert cleanup.validate_path(target, registering=True)[0] == target
    with pytest.raises(ValueError):
        cleanup.validate_path('/home', registering=True)
