"""Private review cleanup keeps live workspaces and unrelated files intact."""
import importlib.util
import os
from pathlib import Path
import sys

import pytest


@pytest.fixture
def cleanup(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / 'scripts/test-artifacts-cleanup.py'
    spec = importlib.util.spec_from_file_location('test_artifacts_cleanup_script', script)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    repository = tmp_path / 'checkout'
    (repository/'build').mkdir(parents=True)
    monkeypatch.setattr(module, 'REPOSITORY', repository)
    return module


def test_only_recognized_review_snapshots_are_eligible(cleanup):
    for name in ('review748-example', 'review-browser-fixes4-example'):
        path = cleanup.REPOSITORY/'build'/name
        path.mkdir()
        assert cleanup.validate_path(path)[0] == path
    for name in ('important', 'review-unrelated', 'review748'):
        path = cleanup.REPOSITORY/'build'/name
        path.mkdir()
        with pytest.raises(ValueError):
            cleanup.validate_path(path)


def test_managed_cleanup_delegates_lifetime_checks_and_rejects_baselines(cleanup, monkeypatch):
    volume = cleanup.REPOSITORY / 'build/library-image-poc/volume'
    workspace = volume / 'workspaces' / ('a' * 32)
    workspace.mkdir(parents=True)
    for path in (volume, workspace.parent, volume / 'baselines' / ('b' * 32)):
        path.mkdir(parents=True, exist_ok=True)
        with pytest.raises(ValueError):
            cleanup.validate_path(path)
    monkeypatch.setattr(cleanup, 'managed_cleanup', lambda *args, **kwargs: [])
    assert cleanup.main(['--delete', str(workspace)]) == 1
    assert workspace.is_dir()
    def recover(path, *, delete):
        if delete:
            path.rmdir()
        return [str(path)]
    monkeypatch.setattr(cleanup, 'managed_cleanup', recover)
    assert cleanup.main([str(workspace)]) == 0
    assert workspace.is_dir()
    assert cleanup.main(['--delete', str(workspace)]) == 0
    assert not workspace.exists()


def test_a_live_file_reference_blocks_private_snapshot_cleanup(cleanup):
    path = cleanup.REPOSITORY/'build'/'review748-example'
    path.mkdir()
    active = cleanup.Process(os.getpid()+100, '1', ('python',), (str(path/'live.db'),))
    assert 'open file' in cleanup.blocker(path, [active])


def test_other_test_runs_do_not_block_an_unreferenced_private_snapshot(cleanup, tmp_path):
    path = cleanup.REPOSITORY/'build'/'review748-example'
    path.mkdir()
    other = cleanup.Process(os.getpid()+100, '1', ('pytest', 'other.py'), ('/elsewhere',))
    assert cleanup.blocker(path, [other]) is None
    legacy = tmp_path/'tuneconsole-review-example'/'runtime-example'
    legacy.mkdir(parents=True)
    assert 'unregistered workspace' in cleanup.blocker(legacy, [other])


def test_deletion_preserves_reports_and_unrelated_build_files(cleanup, monkeypatch):
    path = cleanup.REPOSITORY/'build'/'review748-example'
    path.mkdir()
    (path/'fixture.db').write_bytes(b'disposable')
    report = cleanup.REPOSITORY/'acceptance.json'
    report.write_text('retained')
    unrelated = cleanup.REPOSITORY/'build'/'important'
    unrelated.mkdir()
    monkeypatch.setattr(cleanup, 'processes', lambda: [])
    assert cleanup.main(['--delete', str(path)]) == 0
    assert not path.exists()
    assert report.read_text() == 'retained'
    assert unrelated.is_dir()


def test_only_individual_named_fixture_bases_are_eligible(cleanup):
    root = cleanup.REPOSITORY / 'build' / 'pytest'
    root.mkdir()
    base = root / ('run-12345-' + 'a' * 32)
    base.mkdir()
    assert cleanup.validate_path(base)[0] == base
    for path in (root, root / 'important', root / 'run-12345-short'):
        path.mkdir(exist_ok=True)
        with pytest.raises(ValueError):
            cleanup.validate_path(path)


def test_legacy_schema_cleanup_preserves_live_databases_and_sibling_reports(cleanup, monkeypatch):
    root = cleanup.REPOSITORY / 'build' / 'schema-acceptance'
    workspace = root / 'schema490-xfeb93y_'
    workspace.mkdir(parents=True)
    database = workspace / 'annotations.db'
    database.write_bytes(b'disposable')
    report = root / 'report.json'
    report.write_text('retained')
    unrelated = root / 'important'
    unrelated.mkdir()
    malformed = root / 'schema490-short'
    malformed.mkdir()
    for path in (root, unrelated, malformed):
        with pytest.raises(ValueError):
            cleanup.validate_path(path)
    active = cleanup.Process(os.getpid() + 100, '1', ('python',), (str(database),))
    monkeypatch.setattr(cleanup, 'processes', lambda: [active])
    assert cleanup.main(['--delete', str(workspace)]) == 1
    assert database.read_bytes() == b'disposable'
    monkeypatch.setattr(cleanup, 'processes', lambda: [])
    assert cleanup.main([str(workspace)]) == 0
    assert database.exists()
    assert cleanup.main(['--delete', str(workspace)]) == 0
    assert not workspace.exists()
    assert report.read_text() == 'retained'
    assert unrelated.is_dir() and malformed.is_dir()


def test_live_fixture_owner_blocks_cleanup_even_without_open_files(cleanup):
    base = cleanup.REPOSITORY / 'build' / 'pytest' / ('run-12345-' + 'a' * 32)
    active = cleanup.Process(12345, '1', ('pytest', 'test_probe.py'), ())
    assert 'owner process 12345' in cleanup.blocker(base, [active])


@pytest.mark.parametrize('unreadable', [False, True])
def test_fixture_cleanup_keeps_unknown_or_referenced_workspaces(cleanup, monkeypatch, unreadable):
    base = cleanup.REPOSITORY / 'build' / 'pytest' / ('run-12345-' + 'a' * 32)
    base.mkdir(parents=True)
    database = base / 'fixture.db'
    database.write_bytes(b'preserved')
    other = cleanup.Process(54321, '1', ('python',), (str(database),), unreadable=unreadable)
    monkeypatch.setattr(cleanup, 'processes', lambda: [other])
    assert cleanup.main(['--delete', str(base)]) != 0
    assert database.read_bytes() == b'preserved'


def test_inactive_fixture_cleanup_preserves_other_sessions_and_reports(cleanup, monkeypatch):
    root = cleanup.REPOSITORY / 'build' / 'pytest'
    inactive, active = [root / (f'run-{pid}-' + 'a' * 32) for pid in (12345, 54321)]
    for path in (inactive, active):
        path.mkdir(parents=True)
        (path / 'fixture.db').write_bytes(b'disposable')
    report = root / 'report.json'
    report.write_text('retained')
    other = cleanup.Process(54321, '1', ('pytest', 'other.py'), (str(active / 'fixture.db'),))
    monkeypatch.setattr(cleanup, 'processes', lambda: [other])
    assert cleanup.main(['--delete', str(inactive)]) == 0
    assert not inactive.exists()
    assert active.is_dir() and report.read_text() == 'retained'
    assert cleanup.main(['--delete', str(active)]) != 0
    assert active.is_dir()


@pytest.mark.parametrize('command', [('/usr/lib/systemd/systemd', '--user'), ('(sd-pam)',), ('sway',), ()])
def test_unrelated_unreadable_process_does_not_veto_owned_workspace(cleanup, monkeypatch, command):
    base = cleanup.REPOSITORY / 'build' / 'pytest' / ('run-12345-' + 'a' * 32)
    base.mkdir(parents=True)
    (base / 'fixture.db').write_bytes(b'disposable')
    unrelated = cleanup.Process(1326, '2246', command, (), unreadable=True)
    monkeypatch.setattr(cleanup, 'processes', lambda: [unrelated])
    assert cleanup.main([str(base)]) == 0
    assert base.is_dir()
    assert cleanup.main(['--delete', str(base)]) == 0
    assert not base.exists()


def test_unreadable_process_still_blocks_unknown_workspace_ownership(cleanup, tmp_path):
    base = tmp_path / 'tuneconsole-legacy-workspace'
    base.mkdir()
    unreadable = cleanup.Process(1326, '2246', ('sway',), (), unreadable=True)
    assert cleanup.blocker(base, [unreadable]) == 'cannot inspect process 1326'


def test_unreadable_test_worker_still_blocks_owned_workspace(cleanup):
    base = cleanup.REPOSITORY / 'build' / 'pytest' / ('run-12345-' + 'a' * 32)
    worker = cleanup.Process(54321, '1', ('pytest', 'test_example.py'), (), unreadable=True)
    assert cleanup.blocker(base, [worker]) == 'cannot inspect process 54321'


def test_unreadable_owner_still_blocks_its_own_fixture(cleanup):
    base = cleanup.REPOSITORY / 'build' / 'pytest' / ('run-1326-' + 'a' * 32)
    owner = cleanup.Process(1326, '', (), (), unreadable=True)
    assert 'owner process 1326' in cleanup.blocker(base, [owner])


@pytest.mark.parametrize('reference', ['cwd', 'file'])
def test_process_keeps_references_observed_before_a_denied_read(cleanup, tmp_path, monkeypatch, reference):
    root = tmp_path / 'proc'
    entry = root / '1326'
    entry.mkdir(parents=True)
    (entry / 'stat').write_text('1326 (worker) S ' + '0 ' * 18 + '2246')
    (entry / 'cmdline').write_bytes(b'python\0worker.py\0')
    base = cleanup.REPOSITORY / 'build' / 'pytest' / ('run-12345-' + 'a' * 32)
    base.mkdir(parents=True)
    target = str(base if reference == 'cwd' else base / 'fixture.db')
    (entry / 'cwd').symlink_to(base if reference == 'cwd' else tmp_path, target_is_directory=True)
    (entry / 'fd').mkdir()
    if reference == 'file':
        (entry / 'fd' / '1').symlink_to(target)
    # Deny maps after cwd/fd references have already been collected.
    original = cleanup.Path.read_text

    def read_text(path, *args, **kwargs):
        if path == entry / 'maps':
            raise PermissionError('mappings unavailable')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(cleanup.Path, 'read_text', read_text)
    observed = cleanup.processes(root)
    assert observed[0].unreadable
    assert observed[0].start == '2246' and observed[0].command == ('python', 'worker.py')
    assert target in observed[0].references
    assert 'open file, mapping or cwd' in cleanup.blocker(base, observed)


def test_observed_mapping_still_blocks(cleanup):
    base = cleanup.REPOSITORY / 'build' / 'pytest' / ('run-12345-' + 'a' * 32)
    process = cleanup.Process(1326, '2246', ('python',), (str(base / 'mapped.db'),))
    assert 'open file, mapping or cwd' in cleanup.blocker(base, [process])


@pytest.mark.parametrize('observed_start', ['2246', ''])
def test_registered_unreadable_owner_and_pid_reuse(cleanup, tmp_path, observed_start):
    base = tmp_path / 'tuneconsole-registered-workspace'
    base.mkdir()
    owner = cleanup.Process(1326, '2246', ('python', 'experiment.py'), ())
    cleanup.register(base, owner.pid, [owner])
    owner.unreadable, owner.start = True, observed_start
    assert 'owner process 1326' in cleanup.blocker(base, [owner])
    owner.start = '9999'
    assert cleanup.blocker(base, [owner]) is None


def test_image_cleanup_accepts_only_individual_proof_workspaces(cleanup):
    volume = cleanup.REPOSITORY / 'build/library-image-poc/volume'
    proof = volume / 'proof-abcd1234'
    proof.mkdir(parents=True)
    assert cleanup.validate_path(proof)[0] == proof
    with pytest.raises(ValueError):
        cleanup.validate_path(volume)
    unknown = volume / 'important'
    unknown.mkdir()
    with pytest.raises(ValueError):
        cleanup.validate_path(unknown)


def test_image_cleanup_does_not_thaw_a_live_baseline(cleanup, monkeypatch):
    proof = cleanup.REPOSITORY / 'build/library-image-poc/volume/proof-abcd1234'
    baseline = proof / 'baseline'
    baseline.mkdir(parents=True)
    active = cleanup.Process(os.getpid() + 100, '1', ('python',), (str(baseline / 'state.db'),))
    monkeypatch.setattr(cleanup, 'processes', lambda: [active])
    calls = []
    monkeypatch.setattr(cleanup.subprocess, 'run', lambda *args, **kwargs: calls.append(args))
    assert cleanup.main(['--delete', str(proof)]) == 1
    assert baseline.is_dir() and calls == []


def test_image_cleanup_thaws_only_before_deleting_disposable_subvolumes(cleanup, monkeypatch):
    proof = cleanup.REPOSITORY / 'build/library-image-poc/volume/proof-abcd1234'
    baseline = proof / 'baseline'
    baseline.mkdir(parents=True)
    (baseline / 'state.db').write_bytes(b'disposable')
    monkeypatch.setattr(cleanup, 'processes', lambda: [])
    calls = []
    monkeypatch.setattr(cleanup.subprocess, 'run', lambda args, **kwargs: calls.append(args))
    assert cleanup.main([str(proof)]) == 0
    assert calls == [] and baseline.is_dir()
    assert cleanup.main(['--delete', str(proof)]) == 0
    assert calls == [
        ['btrfs', 'property', 'set', '-ts', str(baseline), 'ro', 'false'],
        ['btrfs', 'subvolume', 'delete', str(baseline)],
    ]
    assert not proof.exists()
