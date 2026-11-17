"""Captured measurements retain exact usable input without the producer tree."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import time

import pytest

from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.test_audio_vibes import sample
from tests import file_custody_support as custody
from tests.store_fixture_support import file_consumer_store, native_sql_calls, without_query_timing
from yt_playlist.core import db_handles, fao, sample_files
from yt_playlist.core.db_rebuild import job
from yt_playlist.core.policy_documents import coordinate_updates
from yt_playlist.core.store import Store
from yt_playlist.repos.audio_vibes import install_sample_files


pytestmark = pytest.mark.usefixtures('without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Sample custody retains SQL, files and restart without HTTP startup."""


def capture(store):
    # Supply the bounded new owner over each retained numbered layout. Startup
    # and additive-DDL tests separately own admission and unchanged old bytes.
    with store.conn.atomic():
        install_sample_files(store.conn)
    values = [sample(run_id='captured-run', point=point)
              for point in ('thirty', 'middle', 'two_thirds')]
    for index, value in enumerate(values):
        store.audio_vibes.save_sample(value, index + 10)
    return values


@pytest.mark.parametrize('failure', ['file', 'status'])
def test_interrupted_sample_commits_its_id_before_file_and_finishes_an_identical_retry_after_restart(
        file_consumer_store, tmp_path, monkeypatch, failure):
    store = file_consumer_store
    value = sample()
    owner, method = ((store.audio_vibes.sample_files, 'publish_reserved') if failure == 'file' else
                     (store.audio_vibes.repository, 'complete_sample'))

    def interrupted(*args):
        raise OSError('sample interruption')

    with monkeypatch.context() as patch:
        patch.setattr(owner, method, interrupted)
        with pytest.raises(OSError, match='sample interruption'):
            store.audio_vibes.save_sample(value, 10)
    with closing(sqlite3.connect(store._db_path)) as observer:
        observer.row_factory = sqlite3.Row
        pending = dict(observer.execute('SELECT * FROM sample_attributes').fetchone())
    assert pending['id'] == value['id'] and pending['file_status'] == 'pending'
    assert Path(pending['filename']).stem == value['id']
    assert Path(pending['filename']).exists() is (failure == 'status')
    assert not store.audio_vibes.samples(value['run_id'])
    assert not store.audio_vibes.sample_receipts(value['run_id'])
    assert not store.audio_vibes.repository.sample_owners([value['id']])

    with store.conn:
        store.conn.execute('BEGIN')
        manifest = sample_files.export(store.conn, tmp_path / 'backup')
    isolated = tmp_path / 'isolated' / 'copy.db'
    isolated.parent.mkdir()
    with closing(sqlite3.connect(isolated)) as target:
        target.row_factory = sqlite3.Row
        store.conn.backup(target)
        sample_files.restore(target, manifest)
        copied = dict(target.execute('SELECT * FROM sample_attributes').fetchone())
        assert copied['file_status'] == 'pending'
        assert Path(copied['filename']).is_relative_to(isolated.parent)
        assert Path(copied['filename']).exists() is (failure == 'status')
        if failure == 'status':
            assert fao.read(copied['filename']).bytes == sample_files.canonical(value)
    # Maintenance inventories count usable files; an interrupted reservation
    # without a file must not make compaction fail during its initial report.
    job._owned_files(store.conn)

    restarted = Store(store._db_path)
    try:
        restarted.init_schema(defer_metadata=True)
        restarted.audio_vibes.save_sample(value, 20)
        assert restarted.audio_vibes.samples(value['run_id']) == [value]
        assert restarted.audio_vibes.sample_receipts(value['run_id']) == [
            dict(id=value['id'], point=value['point'])]
        retained = restarted.conn.execute('SELECT * FROM sample_attributes').fetchone()
        assert retained['file_status'] == 'ready' and retained['captured_at'] == 10
        assert retained['filename'] == pending['filename']
        assert not restarted.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        restarted.conn.close()


def test_installed_hash_named_sample_keeps_reading_and_retrying_beside_new_id_named_samples(
        file_consumer_store):
    store = file_consumer_store
    old = sample()
    store.audio_vibes.save_sample(old, 10)
    filename = sample_files.publish(sample_files.directory(store.conn), old)
    with store.conn.atomic():
        with coordinate_updates(store.conn, ['sample_attributes']):
            store.conn.execute('UPDATE sample_attributes SET filename=?,content_digest=NULL WHERE id=?',
                               (filename, old['id']))
    original = Path(filename).read_bytes()
    store.init_schema(defer_metadata=True)
    assert Path(filename).read_bytes() == original
    assert store.audio_vibes.samples(old['run_id']) == [old]
    store.audio_vibes.save_sample(old, 20)
    newer = sample()
    store.audio_vibes.save_sample(newer, 30)
    references = {row['id']: dict(row) for row in store.conn.execute('SELECT * FROM sample_attributes')}
    assert references[old['id']]['filename'] == filename
    assert Path(references[newer['id']]['filename']).stem == newer['id']
    assert store.audio_vibes.samples(newer['run_id']) == [newer]


def test_sample_expiry_retains_ready_files_and_active_writes_but_releases_abandoned_pending_files(
        file_consumer_store, monkeypatch):
    store = file_consumer_store
    ready, pending = sample(), sample()
    store.audio_vibes.save_sample(ready, 10)
    def interrupted(*args):
        raise OSError('status interruption')
    with monkeypatch.context() as patch:
        patch.setattr(store.audio_vibes.repository, 'complete_sample', interrupted)
        with pytest.raises(OSError, match='status interruption'):
            store.audio_vibes.save_sample(pending, 20)
    paths = {row['id']: Path(row['filename']) for row in store.conn.execute('SELECT * FROM sample_attributes')}
    root = sample_files.directory(store.conn)
    now = time.time() + 31 * 86400

    def collect():
        work = store.retention.managed
        work.close()
        work.contracts = [(root, 'samples')]
        removed = 0
        try:
            while True:
                removed += work.maintain(now)
                if store.retention.database._managed_file_orphans.paths is None:
                    return removed
        finally:
            work.close()

    with store.audio_vibes.sample_files.retained():
        assert collect() == 0
    assert paths[pending['id']].exists()
    assert collect() == 1
    assert paths[ready['id']].exists() and not paths[pending['id']].exists()
    assert store.conn.execute('SELECT file_status FROM sample_attributes WHERE id=?',
                              (pending['id'],)).fetchone()[0] == 'pending'
    store.audio_vibes.save_sample(pending, 30)
    assert store.audio_vibes.samples(pending['run_id']) == [pending]


@pytest.mark.parametrize('installed', [107], indirect=True)
@pytest.mark.parametrize('kind', ['restored', 'snapshot'])
def test_public_recovery_retains_samples_without_producer(installed, tmp_path, kind, native_sql_calls):
    store, _ = installed
    values = capture(store)
    original = list(store.conn.iterdump())
    root = sample_files.directory(store.conn)
    hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in root.rglob('*.json')}
    clocks = {row[0]: (fao.read(row[1]).info.mtime_ns, fao.read(row[1]).info.ctime_ns)
              for row in store.conn.execute('SELECT id,filename FROM sample_attributes')}
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
            for identity, filename, digest in candidate.conn.execute('SELECT id,filename,content_digest FROM sample_attributes'):
                assert Path(filename).is_relative_to(sample_files.directory(candidate.conn))
                assert Path(filename).stem == identity
                assert hashlib.sha256(fao.read(filename).bytes).hexdigest() == digest
                copied = fao.read(filename).info
                assert (copied.mtime_ns, copied.ctime_ns) == clocks[identity]
        finally:
            candidate.conn.close()
    finally:
        hidden.rename(root)
    assert list(store.conn.iterdump()) == original
    assert {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*.json')} == hashes


@pytest.mark.parametrize('installed', [107], indirect=True)
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


@pytest.mark.parametrize('installed', [107], indirect=True)
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
        method = '_publish' if operation == 'export' else '_publish_record'
        original_publish = getattr(sample_files, method)
        def interrupted(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise InterruptedError('controlled sample interruption')
            return original_publish(*args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(sample_files, method, interrupted)
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


@pytest.mark.parametrize('installed', [107], indirect=True)
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


@pytest.mark.parametrize('installed', [107], indirect=True)
def test_noncanonical_captured_bytes_survive_and_export_is_cancellable(installed, tmp_path, monkeypatch):
    import json
    store, _ = installed
    value = capture(store)[0]
    encoded = json.dumps(value, indent=2).encode() + b'\n'
    filename = Path(sample_files._publish(sample_files.directory(store.conn), encoded))
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        with coordinate_updates(store.conn, ['sample_attributes']):
            store.conn.execute('UPDATE sample_attributes SET filename=?,content_digest=? WHERE id=?',
                               (str(filename), hashlib.sha256(encoded).hexdigest(), value['id']))
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
