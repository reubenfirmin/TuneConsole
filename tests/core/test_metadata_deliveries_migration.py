"""Delivery identity, exact content reuse, and complete file custody on restore."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import shutil
import sqlite3
from threading import Barrier

import pytest

from yt_playlist.core import artists, fao, metadata_deliveries as deliveries, metadata_delivery_files as files, policy_documents
from yt_playlist.core.migrations import v074_artist_credits as credits, v075_metadata_deliveries as migration
from yt_playlist.core.migrations.runner import Context
from yt_playlist.core.store import Store


@pytest.fixture
def store(tmp_path, _pristine_installed_schema):
    store = Store(tmp_path / 'metadata.db')
    try:
        _pristine_installed_schema(69).backup(store.conn)
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            credits.apply(store.conn, Context(4))
            migration.apply(store.conn, Context(4))
        migration.validate(store.conn)
        store._bind_repositories()
        yield store
    finally:
        store.conn.close()


def delivery(store, key='capture:1', *, video='video', **kwargs):
    entry = store.tracks.upsert_track(video, 'Song', 'First', None, 200,
                                      credits=[{'name': 'First', 'id': 'first'}])
    row = store.conn.execute('SELECT recording_id,video_id FROM catalog_entries WHERE id=?', (entry,)).fetchone()
    return deliveries.Delivery('youtube', key, 'item', video, 20, entry, row[0], row[1], **kwargs)


def record(store, *values):
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        return deliveries.record_many(store.conn, values)


def refs(store):
    return list(map(tuple, files.references(store.conn)))


def export(store, root):
    with policy_documents.retained_reader(files.directory(store.conn)):
        with store.conn:
            store.conn.execute('BEGIN')
            return files.export(store.conn, root)


def test_delivery_replay_keeps_original_capture_and_shares_content_for_independent_captures(store):
    first = delivery(store, response={'artist': 'First', 'nullable': None})
    identity, = record(store, first)
    assert record(store, replace(first, observed_at=99)) == [identity]
    later, = record(store, replace(first, delivery_key='capture:2', observed_at=21))
    assert identity != later
    assert list(map(tuple, store.conn.execute('SELECT observed_at FROM metadata_deliveries ORDER BY id'))) == [(20,), (21,)]
    owned = refs(store)
    assert owned[0][1:] == owned[1][1:]
    assert deliveries.response(store.conn, identity) == first.response
    assert len(list(files.directory(store.conn).glob('*/*.json'))) == 1
    assert Path(owned[0][1]).stat().st_mode & 0o777 == 0o600


def test_provider_scope_and_input_are_part_of_delivery_identity(store):
    first = delivery(store, response=['same'])
    second = delivery(store, video='second', response=['same'])
    result = record(store, first, second, replace(first, provider='independent'),
                    replace(first, provider_entity_type='release'), replace(first, key_contract='provider_revision'))
    assert len(set(result)) == 5
    assert len({row[1:] for row in refs(store)}) == 1


def test_no_raw_response_and_explicit_json_null_are_distinct(store):
    first = delivery(store)
    no_body, null_body = record(store, first, replace(first, delivery_key='null', response=None))
    assert deliveries.response(store.conn, no_body) is deliveries.NO_RESPONSE
    assert deliveries.response(store.conn, null_body) is None
    assert len(refs(store)) == 1
    with pytest.raises(sqlite3.IntegrityError, match='cannot change'):
        record(store, replace(first, response=None))


def test_changed_replay_rolls_back_the_whole_batch(store):
    first = delivery(store, response={'value': 1})
    record(store, first)
    before = list(store.conn.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match='cannot change'):
        record(store, replace(first, delivery_key='new'), replace(first, response={'value': 2}))
    assert list(store.conn.iterdump()) == before


def test_contract_hash_is_not_a_substitute_for_full_identity_comparison(store, monkeypatch):
    first = delivery(store, response={})
    digest = first.contract_digest()
    record(store, first)
    monkeypatch.setattr(deliveries.Delivery, 'contract_digest', lambda self: digest)
    with pytest.raises(sqlite3.IntegrityError, match='cannot change'):
        record(store, replace(first, delivery_key='different'))
    assert store.conn.execute('SELECT count(*) FROM metadata_deliveries').fetchone()[0] == 1


def test_database_rejects_duplicate_delivery_contract_even_with_a_different_digest(store):
    first = delivery(store)
    identity, = record(store, first)
    with pytest.raises(sqlite3.IntegrityError):
        with store.conn:
            store.conn.execute('INSERT INTO metadata_deliveries(provider,key_contract,delivery_key,provider_entity_type,'
                'provider_entity_id,input_entry_id,input_recording_id,input_video_id,observed_at,contract_digest,format_version) '
                'SELECT provider,key_contract,delivery_key,provider_entity_type,provider_entity_id,input_entry_id,'
                'input_recording_id,input_video_id,observed_at,?,format_version FROM metadata_deliveries WHERE id=?',
                ('0' * 64, identity))


def test_handled_insert_failure_cannot_commit_a_partial_delivery_batch(store):
    first = delivery(store, response={})
    store.conn.execute("CREATE TEMP TRIGGER reject_second_delivery BEFORE INSERT ON metadata_deliveries "
                       "WHEN NEW.delivery_key='second' BEGIN SELECT RAISE(ABORT,'controlled insertion failure'); END")
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.IntegrityError, match='controlled insertion'):
            deliveries.record_many(store.conn, [first, replace(first, delivery_key='second')])
    assert store.conn.execute('SELECT count(*) FROM metadata_deliveries').fetchone()[0] == 0
    assert files.collect_orphans(store.conn) == 1


def test_explicit_unknown_historical_capture_time_is_retained(store):
    value = deliveries.Delivery('legacy_provider', 'findings:17', 'legacy_unknown', None, None,
                                 key_contract='legacy_record', response={'original': 'finding'})
    identity, = record(store, value)
    assert store.conn.execute('SELECT observed_at FROM metadata_deliveries WHERE id=?', (identity,)).fetchone()[0] is None
    assert deliveries.response(store.conn, identity) == value.response


@pytest.mark.parametrize('changes', [
    {'provider': ''}, {'delivery_key': ''}, {'delivery_key': 'x' * 4097}, {'provider_entity_type': 'arbitrary'},
    {'provider_entity_id': None}, {'key_contract': 'made_up'}, {'observed_at': None}, {'observed_at': float('inf')},
    {'observed_at': float('nan')}, {'observed_at': True}, {'observed_at': 10 ** 400}, {'input_entry_id': True},
    {'input_entry_id': None}, {'input_recording_id': None}, {'input_video_id': ''}, {'response': {'bad': float('nan')}},
])
def test_invalid_contract_does_not_publish_rows_or_files(store, changes):
    first = delivery(store, response={})
    with pytest.raises((ValueError, TypeError)):
        record(store, replace(first, **changes))
    assert not refs(store)
    assert store.conn.execute('SELECT count(*) FROM metadata_deliveries').fetchone()[0] == 0
    assert not list(files.directory(store.conn).glob('*/*.json'))


def test_bounded_admission_preflights_the_entire_batch(store):
    first = delivery(store, response={})
    with pytest.raises(ValueError, match='owner budget'):
        record(store, *(replace(first, delivery_key=str(i)) for i in range(129)))
    with pytest.raises(ValueError, match='raw response byte budget'):
        record(store, replace(first, response='x' * files.MAX_BYTES))
    assert store.conn.execute('SELECT count(*) FROM metadata_deliveries').fetchone()[0] == 0
    assert not list(files.directory(store.conn).glob('*/*.json'))


def test_input_reassociation_does_not_rewrite_captured_identity(store):
    first = delivery(store, response={})
    identity, = record(store, first)
    other = delivery(store, video='other')
    # Reassociate a source-ref entry, avoiding the item/recording composite FK.
    with store.conn:
        source = store.conn.execute('INSERT INTO catalog_entries(recording_id,source_ref) VALUES (?,?)',
                                    (first.input_recording_id, 'test:source')).lastrowid
    captured = replace(first, input_entry_id=source, input_video_id=None)
    second, = record(store, captured)
    with store.conn:
        store.conn.execute('UPDATE catalog_entries SET recording_id=? WHERE id=?', (other.input_recording_id, source))
    assert record(store, captured) == [second]
    with pytest.raises(ValueError, match='subject changed'):
        record(store, replace(captured, delivery_key='after-reassociation'))
    assert deliveries.response(store.conn, identity) == {}
    assert store.conn.execute('SELECT input_recording_id FROM metadata_deliveries WHERE id=?', (second,)).fetchone()[0] == first.input_recording_id


def test_artist_credit_links_the_actual_delivery_and_rejects_another_owner(store):
    first = delivery(store, response={'artists': [{'name': 'First', 'id': 'first'}]})
    identity, = record(store, first)
    for _ in range(2):
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            artists.observe(store.conn, first.input_entry_id, 'First', first.response['artists'],
                            now=21, metadata_delivery_id=identity)
    row = store.conn.execute('SELECT o.* FROM entry_artist_selections s '
                            'JOIN artist_credit_observations o ON o.id=s.observation_id WHERE s.entry_id=?',
                            (first.input_entry_id,)).fetchone()
    assert row['metadata_delivery_id'] == identity and row['processing_count'] == 2
    assert row['delivery_key'] == f'metadata-delivery:{identity}'
    second = delivery(store, video='other')
    with pytest.raises(ValueError, match='different input or provider'):
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            artists.observe(store.conn, second.input_entry_id, 'First', first.response['artists'],
                            metadata_delivery_id=identity)
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        with store.conn:
            store.conn.execute('UPDATE artist_credit_observations SET metadata_delivery_id=NULL WHERE id=?', (row['id'],))


def test_backup_round_trip_keeps_independent_owners_but_only_one_copy_of_shared_content(store, tmp_path):
    first = delivery(store, response={'same': 'content'})
    for start in range(0, 640, 128):
        record(store, *(replace(first, delivery_key=str(i), observed_at=i) for i in range(start, start + 128)))
    before = refs(store)
    manifest = export(store, tmp_path / 'backup')
    # One response pack, two 512-owner chunks and one manifest.
    assert len(list((tmp_path / 'backup').glob('*/*.json'))) == 4
    assert len({row[1:] for row in before}) == 1
    shutil.rmtree(files.directory(store.conn))
    files.restore(store.conn, manifest)
    after = refs(store)
    assert [(row[0], row[2:4]) for row in after] == [(row[0], row[2:4]) for row in before]
    assert len({row[1:] for row in after}) == 1
    assert len(list(files.directory(store.conn).glob('*/*.json'))) == 1
    assert deliveries.response(store.conn, after[-1][0]) == first.response
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_export_streams_and_preserves_existing_pack_offsets_and_clocks(store, tmp_path, monkeypatch):
    first = delivery(store, response={'value': 0})
    record(store, *(replace(first, delivery_key=str(i), response={'value': i}) for i in range(128)))
    before = refs(store)
    source, = {Path(row[1]) for row in before}
    encoded, age = source.read_bytes(), fao.stat(source)
    opened = 0
    original = fao.open_reader

    @contextmanager
    def counted(path, **kwargs):
        nonlocal opened
        if Path(path) == source:
            opened += 1
        with original(path, **kwargs) as stream:
            yield stream

    monkeypatch.setattr(fao, 'open_reader', counted)
    root = tmp_path / 'backup'
    manifest = export(store, root)
    assert opened == 2  # One range-validation stream and one verified encoded copy.
    copied = root / source.stem[:2] / source.name
    assert copied.read_bytes() == encoded
    assert (fao.stat(copied).mtime_ns, fao.stat(copied).ctime_ns) == (age.mtime_ns, age.ctime_ns)
    document = files._read_document(manifest, 'backup-manifest')
    chunk = files._read_document(root / document['chunks'][0][:2] / (document['chunks'][0] + '.json'), 'backup-chunk')
    assert [(row['id'], row['offset']) for row in chunk] == [(row[0], row[4]) for row in before]
    shutil.rmtree(files.directory(store.conn))
    files.restore(store.conn, manifest)
    assert [deliveries.response(store.conn, row[0]) for row in refs(store)] == [{'value': i} for i in range(128)]


def test_missing_later_backup_record_never_partially_relocates_database(store, tmp_path):
    first = delivery(store, response={'value': 'x' * files.PACK_BYTES})
    record(store, first, replace(first, delivery_key='later', response={'value': 'y' * files.PACK_BYTES}))
    before = refs(store)
    manifest = export(store, tmp_path / 'backup')
    root = Path(manifest).parent.parent
    document = files._read_document(manifest, 'backup-manifest')
    chunk_digest = document['chunks'][0]
    chunk = files._read_document(root / chunk_digest[:2] / (chunk_digest + '.json'), 'backup-chunk')
    (root / chunk[-1]['relative_filename']).unlink()
    with pytest.raises(FileNotFoundError):
        files.restore(store.conn, manifest)
    assert refs(store) == before
    assert deliveries.response(store.conn, before[0][0]) == first.response


def test_restore_owner_mismatch_never_changes_references(store, tmp_path):
    first = delivery(store, response={'value': 1})
    record(store, first)
    manifest = export(store, tmp_path / 'backup')
    record(store, replace(first, delivery_key='later'))
    before = refs(store)
    with pytest.raises(ValueError, match='missing a database owner'):
        files.restore(store.conn, manifest)
    assert refs(store) == before


def test_rollback_files_are_collectible_but_pinned_old_reader_blocks_collection(store):
    first = delivery(store, response={'value': 1})
    record(store, first)
    with pytest.raises(RuntimeError):
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            deliveries.record_many(store.conn, [replace(first, delivery_key='orphan', response={'value': 2})])
            raise RuntimeError('interrupted after file publication')
    assert len(list(files.directory(store.conn).glob('*/*.json'))) == 2
    with store.read_view():
        assert files.collect_orphans(store.conn) == 0
    assert files.collect_orphans(store.conn) == 1
    assert deliveries.response(store.conn, refs(store)[0][0]) == first.response


def test_concurrent_replay_and_restart_retain_one_delivery(store):
    first = delivery(store, response={'value': 1})
    barrier = Barrier(2)

    def admit():
        conn = sqlite3.connect(store._db_path)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            barrier.wait(timeout=10)
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                return deliveries.record_many(conn, [first])[0]
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: admit(), range(2)))
    assert results[0] == results[1]
    conn = sqlite3.connect(store._db_path)
    try:
        conn.row_factory = sqlite3.Row
        assert deliveries.response(conn, results[0]) == first.response
        assert conn.execute('SELECT count(*) FROM metadata_deliveries').fetchone()[0] == 1
        migration.validate(conn)
    finally:
        conn.close()
