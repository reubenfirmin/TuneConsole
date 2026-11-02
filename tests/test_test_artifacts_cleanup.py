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
