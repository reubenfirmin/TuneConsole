"""Exact profile heads survive partial conversion, failed writes and recovery."""
from tests.file_custody_support import export_owner, restore_owner
from tests.store_fixture_support import file_consumer_store
from tests.file_content_support import read_bytes as original_bytes
from tests.file_content_support import patch_byte_reads
import hashlib
import json
import shutil
import sqlite3
from contextlib import closing

import pytest

from yt_playlist.core import fao, profile_files as owner
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.store import Store
from yt_playlist.core.profile_paths import record_path


def payload(value):
    raw = json.dumps({'dimensions': {}, 'evidence': value}, indent=2)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def seed(store, names=('one', 'two'), *, pack_size=None):
    for name in names:
        store.tracks.upsert_track(name, name, 'Artist', '', 100)
    keys = [r[0] for r in store.conn.execute('SELECT identity_key FROM recordings ORDER BY id')]
    rows = [(key, raw, 'v1', digest) for key in keys for raw, digest in (payload(key),)]
    size = pack_size or len(rows)
    for offset in range(0, len(rows), size):
        store.musical_profiles.publish_recordings(rows[offset:offset+size], 1)
    bodies = {key: raw for key, raw, _, _ in rows}
    return keys, bodies


def test_profile_page_batches_heads_and_validates_mixed_publications(store, monkeypatch):
    keys, bodies = seed(store)
    statements, opened = [], []
    original = original_bytes
    def read(path, **options):
        opened.append(path)
        return original(path, **options)
    patch_byte_reads(monkeypatch, read)
    store.conn.set_trace_callback(statements.append)
    try:
        assert owner.read_page(store.conn, 'recording', keys) == {
            key: json.loads(raw) for key, raw in bodies.items()}
    finally:
        store.conn.set_trace_callback(None)
    assert len(opened) == 1
    assert len([sql for sql in statements if sql.startswith('SELECT')]) == 1
    with pytest.raises(Exception, match='byte budget'):
        owner.read_page(store.conn, 'recording', keys, max_bytes=1)
    with atomic_write(store):
        store.conn.execute('UPDATE recordings SET musical_profile_size=musical_profile_size+1 '
                           'WHERE musical_profile_size IS NOT NULL')
    with pytest.raises(fao.CorruptFile, match='exact head'):
        owner.read_page(store.conn, 'recording', keys)


def test_profile_page_reuses_a_chunk_for_interleaved_identity_order(store, monkeypatch):
    keys, bodies = seed(store, ('one', 'two', 'three', 'four', 'five', 'six'), pack_size=2)
    opened = []
    read = original_bytes
    def counted(path, **options):
        opened.append(path)
        return read(path, **options)
    patch_byte_reads(monkeypatch, counted)
    interleaved = [keys[i] for i in (0, 2, 4, 1, 3, 5)]
    assert owner.read_page(store.conn, 'recording', interleaved) == {
        key: json.loads(raw) for key, raw in bodies.items()}
    assert len(opened) == len(set(opened)) == 3


def test_exact_profile_reads_respect_individual_and_work_budgets(store):
    keys, bodies = seed(store, pack_size=1)
    assert [owner.read(store.conn, 'recording', key) for key in keys] == [bodies[key] for key in keys]
    assert len(list(owner.references(store.conn))) == 2
    assert store.musical_profiles.by_keys(keys) == {key: json.loads(bodies[key]) for key in keys}
    with pytest.raises(fao.FileBudgetExceeded):
        owner.read(store.conn, 'recording', keys[0], max_bytes=1)
    with pytest.raises(Exception, match='work budget'):
        owner.read(store.conn, 'recording', keys[0], cache=owner.Cache(work_bytes=1))


def test_cached_profile_page_keeps_file_and_caller_budget_checks(store, monkeypatch):
    keys, bodies = seed(store)
    assert owner.read_page(store.conn, 'recording', keys) == {
        key: json.loads(body) for key, body in bodies.items()}
    with monkeypatch.context() as patch:
        patch_byte_reads(patch, lambda *a, **k: pytest.fail('Unchanged chunk was reopened'))
        assert owner.read_page(store.conn, 'recording', keys)[keys[0]] == json.loads(bodies[keys[0]])
        with pytest.raises(Exception, match='work budget'):
            owner.read(store.conn, 'recording', keys[0], cache=owner.Cache(work_bytes=1))
    path = next(owner.references(store.conn))
    fao.write(path, b'{}')
    with pytest.raises(fao.CorruptFile, match='publication'):
        owner.read_page(store.conn, 'recording', keys)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        owner.read_page(store.conn, 'recording', keys)


def test_snapshot_retains_old_body_and_independent_restore(store, tmp_path):
    keys, bodies = seed(store)
    with store.read_view() as snapshot:
        before = owner.read(snapshot.conn, 'recording', keys[0])
        raw, digest = payload('successor')
        store.musical_profiles.publish_recordings([(keys[0], raw, 'v2', digest)], 2)
        assert owner.read(snapshot.conn, 'recording', keys[0]) == before
        assert store.retention.collect_musical_profiles(10**10) == 0
    assert owner.read(store.conn, 'recording', keys[0]) == raw
    store.retention.collect_musical_profiles(10**10)
    assert owner.read(store.conn, 'recording', keys[1]) == bodies[keys[1]]
    manifest = export_owner(store.conn, owner, tmp_path / 'saved')
    restored = Store(tmp_path / 'restored.db')
    try:
        store.conn.backup(restored.conn)
        shutil.rmtree(owner.directory(store.conn))
        restore_owner(restored.conn, 'profiles', owner, manifest)
        restored.init_schema()
        assert restored.musical_profiles.by_keys(keys)[keys[0]] == json.loads(raw)
        path = next(owner.references(restored.conn))
        fao.write(path, b'{}')
        with pytest.raises(fao.CorruptFile):
            restored.musical_profiles.by_keys(keys)
    finally:
        restored.conn.close()


@pytest.mark.parametrize('boundary', ['file', 'status'])
def test_runtime_profile_interruption_keeps_selected_bytes_and_committed_pending_work(file_consumer_store, monkeypatch, boundary):
    from yt_playlist.repos import profile_file_writes
    store = file_consumer_store
    keys, bodies = seed(store)
    original_file = store.conn.execute("SELECT max(file_address) FROM collection_file_writes WHERE kind='profile'").fetchone()[0]
    raw, digest = payload('replacement')
    files = store.musical_profiles.files
    write, complete = files.write_reserved, profile_file_writes.complete
    observed = []

    def interrupted_file(records):
        assert not store.conn.in_transaction
        with closing(sqlite3.connect(store._db_path)) as observer:
            observed.extend(observer.execute("SELECT file_address,file_status FROM collection_file_writes WHERE kind='profile'"))
        raise OSError('file interrupted')

    def interrupted_status(conn, kind, references):
        complete(conn, kind, references)
        raise OSError('status interrupted')

    with monkeypatch.context() as patch:
        if boundary == 'file':
            patch.setattr(files, 'write_reserved', interrupted_file)
        else:
            patch.setattr(profile_file_writes, 'complete', interrupted_status)
        with pytest.raises(OSError, match='interrupted'):
            store.musical_profiles.publish_recordings([(keys[0], raw, 'v2', digest)], 2)
    assert owner.read(store.conn, 'recording', keys[0]) == bodies[keys[0]]
    assert store.conn.execute("SELECT file_status FROM collection_file_writes WHERE kind='profile' AND file_address>?",
                              (original_file,)).fetchone()[0] == 'pending'
    if boundary == 'file':
        assert [status for identity, status in observed if identity > original_file] == ['pending']
    store.musical_profiles.publish_recordings([(keys[0], raw, 'v2', digest)], 3)
    assert owner.read(store.conn, 'recording', keys[0]) == raw
    identity = store.conn.execute('SELECT musical_profile_file_id FROM recordings WHERE identity_key=?',
                                  (keys[0],)).fetchone()[0]
    assert store.conn.execute("SELECT file_status FROM collection_file_writes WHERE kind='profile' AND file_address=?",
                              (identity,)).fetchone()[0] == 'ready'
    assert (owner.directory(store.conn) / record_path('recording', identity)).is_file()
