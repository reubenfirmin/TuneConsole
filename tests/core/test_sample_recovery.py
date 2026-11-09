"""Captured measurements retain exact usable input without the producer tree."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.test_audio_vibes import sample
from tests import file_custody_support as custody
from tests.store_fixture_support import native_sql_calls, without_query_timing
from yt_playlist.core import db_handles, fao, sample_files
from yt_playlist.core.db_rebuild import job
from yt_playlist.core.policy_documents import coordinate_updates
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Sample custody retains SQL, files and restart without HTTP startup."""


def capture(store):
    values = [sample(run_id='captured-run', point=point)
              for point in ('thirty', 'middle', 'two_thirds')]
    for index, value in enumerate(values):
        store.audio_vibes.save_sample(value, index + 10)
    return values


@pytest.mark.parametrize('installed', [79, 107], indirect=True)
@pytest.mark.parametrize('kind', ['restored', 'snapshot'])
def test_public_recovery_retains_samples_without_producer(installed, tmp_path, kind, native_sql_calls):
    store, _ = installed
    values = capture(store)
    original = list(store.conn.iterdump())
    root = sample_files.directory(store.conn)
    hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in root.glob('*/*.json')}
    clocks = {path.stem: (fao.read(path).info.mtime_ns, fao.read(path).info.ctime_ns)
              for path in root.glob('*/*.json')}
    report = custody.snapshot_library(store._db_path, tmp_path/'recovered')
    candidate_path = Path(report['restored'])
    if kind == 'snapshot':
        candidate_path = tmp_path/'separate-restore'/'restored.db'
        candidate_path.parent.mkdir()
        with closing(sqlite3.connect(report['snapshot'])) as source, \
                closing(sqlite3.connect(candidate_path)) as target:
            source.backup(target)
            target.row_factory = sqlite3.Row
            custody.restore_files(target, report['manifests'])
    hidden = root.with_name('hidden-producer-samples')
    root.rename(hidden)
    try:
        candidate = Store(candidate_path)
        try:
            candidate.conn.execute('PRAGMA synchronous=OFF')
            candidate.init_schema(defer_metadata=True)
            assert candidate.audio_vibes.samples('captured-run') == values
            assert candidate.conn.execute('SELECT count(*) FROM sample_attributes').fetchone()[0] == 3
            assert not candidate.conn.execute('PRAGMA foreign_key_check').fetchall()
            for filename, in candidate.conn.execute('SELECT filename FROM sample_attributes'):
                assert Path(filename).is_relative_to(sample_files.directory(candidate.conn))
                assert hashlib.sha256(fao.read(filename).bytes).hexdigest() == Path(filename).stem
                copied = fao.read(filename).info
                assert (copied.mtime_ns, copied.ctime_ns) == clocks[Path(filename).stem]
        finally:
            candidate.conn.close()
    finally:
        hidden.rename(root)
    assert list(store.conn.iterdump()) == original
    assert {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.glob('*/*.json')} == hashes


@pytest.mark.parametrize('installed', [79], indirect=True)
@pytest.mark.parametrize('damage', ['missing', 'corrupt'])
def test_recovery_refuses_unavailable_input_without_success(installed, tmp_path, damage):
    store, _ = installed
    capture(store)
    filename = Path(store.conn.execute('SELECT filename FROM sample_attributes LIMIT 1').fetchone()[0])
    if damage == 'missing':
        filename.unlink()
    else:
        filename.write_bytes(b'corrupt captured input')
    original = list(store.conn.iterdump())
    output = tmp_path/'recovery'
    with pytest.raises((OSError, ValueError)):
        custody.snapshot_library(store._db_path, output)
    assert not (output/'restored.db').exists()
    assert list(store.conn.iterdump()) == original
    assert len(store.audio_vibes.samples('captured-run')) == 2


@pytest.mark.parametrize('installed', [79], indirect=True)
@pytest.mark.parametrize('operation', ['export', 'restore'])
def test_interruption_rolls_back_coordinates_and_retry_preserves_exact_bytes(installed, tmp_path, monkeypatch, operation):
    store, _ = installed
    values = capture(store)
    root = tmp_path/'sample-backup'
    before = list(store.conn.iterdump())
    with store.conn:
        store.conn.execute('BEGIN')
        manifest = sample_files.export(store.conn, root)
    target_path = tmp_path/'restored'/'copy.db'
    target_path.parent.mkdir()
    with closing(sqlite3.connect(target_path)) as target:
        target.row_factory = sqlite3.Row
        store.conn.backup(target)
        target_before = list(target.iterdump())
        calls = 0
        original_publish = sample_files._publish
        def interrupted(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise InterruptedError('controlled sample interruption')
            return original_publish(*args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(sample_files, '_publish', interrupted)
            with pytest.raises(InterruptedError, match='controlled sample'):
                if operation == 'export':
                    with store.conn:
                        store.conn.execute('BEGIN')
                        sample_files.export(store.conn, root)
                else:
                    sample_files.restore(target, manifest)
        assert list(target.iterdump()) == target_before
        sample_files.restore(target, manifest)
        expected = {value['id']: sample_files.canonical(value) for value in values}
        assert {identity: fao.read(filename).bytes for identity, filename in target.execute(
            'SELECT id,filename FROM sample_attributes')} == expected
    assert list(store.conn.iterdump()) == before


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_compactor_inventory_and_retained_original_recovery_include_samples(installed, tmp_path):
    store, _ = installed
    values = capture(store)
    paths = [Path(row[0]) for row in store.conn.execute('SELECT filename FROM sample_attributes')]
    inventory = job._owned_files(store.conn)
    assert inventory['files'] >= 3
    assert inventory['bytes'] >= sum(path.stat().st_size for path in paths)
    job.run(store)
    state = db_handles.state(store._db_path)
    assert state['phase'] == 'complete'
    candidate_path = tmp_path/'backup-restore'/'copy.db'
    candidate_path.parent.mkdir()
    # The current job retains the original database and immutable file owners.
    # Recover that original through the public exporter, then remove the shared
    # producer files so a surviving absolute reference cannot hide missing custody.
    with closing(sqlite3.connect(Path(state['original']).as_uri()+'?mode=ro', uri=True)) as backup, \
            closing(sqlite3.connect(candidate_path)) as target:
        backup.row_factory = sqlite3.Row
        with job._files(backup), backup:
            backup.execute('BEGIN')
            manifests = custody.export_files(backup, candidate_path)
        backup.backup(target)
        target.row_factory = sqlite3.Row
        assert 'samples' in manifests
        custody.restore_files(target, manifests)
    hidden = sample_files.directory(store.conn).with_name('hidden-producer-samples')
    sample_files.directory(store.conn).rename(hidden)
    try:
        candidate = Store(candidate_path)
        try:
            candidate.init_schema(defer_metadata=True)
            assert candidate.audio_vibes.samples('captured-run') == values
        finally:
            candidate.conn.close()
    finally:
        hidden.rename(sample_files.directory(store.conn))


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_noncanonical_captured_bytes_survive_and_export_is_cancellable(installed, tmp_path, monkeypatch):
    import json
    store, _ = installed
    value = capture(store)[0]
    encoded = json.dumps(value, indent=2).encode() + b'\n'
    filename = Path(sample_files._publish(sample_files.directory(store.conn), encoded))
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        with coordinate_updates(store.conn, ['sample_attributes']):
            store.conn.execute('UPDATE sample_attributes SET filename=? WHERE id=?', (str(filename), value['id']))
    root = tmp_path/'exact-backup'
    with monkeypatch.context() as patch:
        def cancelled():
            raise InterruptedError('sample cancellation')
        patch.setattr(sample_files, 'check_cancellable_work', cancelled)
        with pytest.raises(InterruptedError, match='sample cancellation'), store.conn:
            store.conn.execute('BEGIN')
            sample_files.export(store.conn, root)
    with store.conn:
        store.conn.execute('BEGIN')
        manifest = sample_files.export(store.conn, root)
    restored = tmp_path/'independent'/'copy.db'
    restored.parent.mkdir()
    with closing(sqlite3.connect(restored)) as target:
        target.row_factory = sqlite3.Row
        store.conn.backup(target)
        sample_files.restore(target, manifest)
        target_filename = target.execute('SELECT filename FROM sample_attributes WHERE id=?', (value['id'],)).fetchone()[0]
        assert fao.read(target_filename).bytes == encoded
    with pytest.raises(ValueError, match='byte budget'):
        sample_files.read(filename, max_bytes=8)
