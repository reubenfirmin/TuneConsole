"""Independent album custody retains source precision and frozen choices."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_predecessor_writes import installed
from tests.independent_schema_support import independent_regular_ddl
from yt_playlist.core import albums, policy_documents
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import album_occurrences as rows, album_shares as shares
from yt_playlist.core.store import Store


def finish(job, conn, size=128):
    for _ in range(150):
        progress = job.step(conn, batch_size=size)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Album preparation did not finish bounded fixture')


def seed(store, count=3):
    entry = store.tracks.upsert_track('one', 'One', 'Artist', None, 10, album_browse_id='release')
    album = albums.lookup(store.conn, 'release')
    with store.conn:
        store.conn.executemany("INSERT INTO album_tracks(album_id,entry_id,position,is_full,source) VALUES (?,?,NULL,1,'legacy')", [(album, entry)] * 2)
        store.conn.execute("INSERT INTO album_tracks(album_id,legacy_track_key,is_full,source) VALUES (?, ?,1,'legacy')", (album, 'missing|recording'))
        store.conn.execute('INSERT INTO album_fetch_state VALUES (?,NULL,1,3)', (album,))
        store.conn.execute('INSERT INTO album_share_snapshots VALUES (?,?,?)', ('release', 'Frozen 東京', json.dumps(['one','missing','one'] if count == 3 else ['one'] * count, indent=2)))
    return album, entry


@pytest.mark.parametrize('installed', [79, 93, 98], indirect=True)
def test_exact_rows_precision_missing_sources_and_frozen_order(installed):
    store, _ = installed
    album, entry = seed(store)
    original = {row['id']: dict(row) for row in store.conn.execute('SELECT * FROM main.album_tracks')}
    for job in (rows.JOB, shares.JOB):
        job.install(store.conn)
        finish(job, store.conn, size=2)
    for row in store.conn.execute('SELECT * FROM prepared_album_track_rows'):
        assert rows.original(store.conn, 'album_tracks', row['row_id'], row['source_digest']) == original[row['row_id']]
        assert row['position'] is None
    assert store.conn.execute('SELECT count(*) FROM prepared_album_track_rows WHERE is_full=0').fetchone()[0] == 1
    unknown = store.conn.execute("SELECT * FROM prepared_album_track_rows WHERE raw_identity_key='missing|recording'").fetchone()
    assert (unknown['recording_id'], unknown['resolution_status']) == (None, 'unresolved')
    state = store.conn.execute('SELECT * FROM prepared_album_fetch_rows').fetchone()
    assert (state['album_id'], state['fetched_at'], state['complete'], state['expected_track_count'], state['provider_id_snapshot']) == (album, None, 1, 3, 'release')
    captured = shares.head(store.conn, 'release')
    raw = dict(store.conn.execute('SELECT * FROM main.album_share_snapshots').fetchone())
    assert shares.read_original(store.conn, captured) == raw
    choices = store.conn.execute('SELECT raw_source_ref,entry_id,resolution_status FROM prepared_album_share_items ORDER BY position').fetchall()
    assert list(map(tuple, choices)) == [('one', entry, 'resolved'), ('missing', None, 'unresolved'), ('one', entry, 'resolved')]
    assert not store.conn.execute("SELECT 1 FROM youtube_items WHERE video_id='missing'").fetchone()
    assert store.conn.execute('SELECT count(*) FROM catalog_entries').fetchone()[0] == 1
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed', [79, 98], indirect=True)
def test_frozen_upgrade_agrees_without_losing_originals(installed, tmp_path):
    store, _ = installed
    seed(store)
    for job in (rows.JOB, shares.JOB):
        job.install(store.conn)
        finish(job, store.conn)
    upgraded = Store(tmp_path / 'album-upgrade.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS)
        for staged in store.conn.execute('SELECT * FROM prepared_album_track_rows WHERE is_full=1'):
            native = upgraded.conn.execute('SELECT * FROM album_track_occurrences WHERE legacy_row_id=?', (staged['row_id'],)).fetchone()
            for field in ('recording_id','captured_title','captured_credit','raw_identity_key','raw_source_ref','position','disc_number','track_number','captured_source','resolution_status'):
                assert native[field] == staged[field]
        for staged in store.conn.execute('SELECT * FROM prepared_album_track_rows WHERE is_full=0'):
            native = upgraded.conn.execute('SELECT * FROM album_item_observations WHERE legacy_row_id=?', (staged['row_id'],)).fetchone()
            assert (native['entry_id'], native['recording_id'], native['raw_identity_key']) == (staged['entry_id'], staged['captured_recording_id'], staged['raw_identity_key'])
        assert [tuple(row) for row in upgraded.conn.execute('SELECT position,raw_source_ref,entry_id FROM album_share_items ORDER BY position')] == [
            tuple(row) for row in store.conn.execute('SELECT position,raw_source_ref,entry_id FROM prepared_album_share_items ORDER BY position')]
        assert shares.read_original(upgraded.conn, shares.head(upgraded.conn, 'release'))['title'] == 'Frozen 東京'
        assert not upgraded.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        upgraded.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_bounded_row_batches_and_reference_updates_reconcile_without_changing_sources(installed):
    store, _ = installed
    album, entry = seed(store)
    with store.conn:
        store.conn.executemany("INSERT INTO album_tracks(album_id,entry_id,position,is_full,source) VALUES (?,?,?,1,'captured')",
                               [(album, entry, index) for index in range(1040)])
    rows.JOB.install(store.conn)
    assert rows.JOB.step(store.conn, batch_size=512).processed == 32
    finish(rows.JOB, store.conn)
    assert store.conn.execute('SELECT count(*) FROM prepared_album_track_rows').fetchone()[0] == 1044
    with store.conn:
        store.conn.execute("INSERT INTO album_tracks(album_id,entry_id,position,is_full,source) VALUES (?,?,1041,1,'later')", (album,entry))
    finish(rows.JOB, store.conn)
    with store.conn:
        store.conn.execute("UPDATE main.youtube_items SET title='New title' WHERE video_id='one'")
    assert rows.JOB.progress(store.conn).prepared_revision is None
    finish(rows.JOB, store.conn)
    assert {row[0] for row in store.conn.execute('SELECT captured_title FROM prepared_album_track_rows WHERE entry_id=?', (entry,))} == {'New title'}
    old = store.conn.execute('SELECT id FROM main.album_tracks WHERE is_full=1 ORDER BY id LIMIT 1').fetchone()[0]
    with store.conn:
        store.conn.execute('DELETE FROM main.album_tracks WHERE id=?', (old,))
    finish(rows.JOB, store.conn)
    assert not store.conn.execute('SELECT 1 FROM prepared_album_track_rows WHERE row_id=?', (old,)).fetchone()
    assert store.conn.execute('SELECT 1 FROM prepared_album_row_originals WHERE owner=? AND source_key=?', ('album_tracks', json.dumps([old], separators=(',', ':')))).fetchone()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_large_share_resume_without_payload_and_continuing_replacement(installed):
    store, _ = installed
    seed(store, count=1040)
    shares.JOB.install(store.conn)
    shares.JOB.step(store.conn)
    def authorizer(action, owner, column, *unused):
        if action == sqlite3.SQLITE_READ and owner == 'album_share_snapshots' and column in ('title','video_ids_json'):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    store.conn.set_authorizer(authorizer)
    try:
        while isinstance(json.loads(shares.JOB.progress(store.conn).cursor), dict):
            shares.JOB.step(store.conn)
    finally:
        store.conn.set_authorizer(None)
    finish(shares.JOB, store.conn)
    old = shares.head(store.conn, 'release')
    assert old['member_count'] == 1040
    with store.conn:
        store.conn.execute("UPDATE main.album_share_snapshots SET video_ids_json='[]' WHERE browse_id='release'")
    finish(shares.JOB, store.conn)
    assert shares.head(store.conn, 'release')['member_count'] == 0
    assert len(json.loads(shares.read_original(store.conn, old)['video_ids_json'])) == 1040


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_failure_interrupt_missing_plan_retry_and_independent_ddl(installed, monkeypatch, independent_regular_ddl):
    store, _ = installed
    seed(store)
    job = shares.JOB
    job.install(store.conn)
    publish = policy_documents._publish_encoded
    def fail(*args, age=None):
        assert not store.conn.in_transaction
        raise OSError('controlled album file failure')
    monkeypatch.setattr(policy_documents, '_publish_encoded', fail)
    with pytest.raises(OSError, match='file failure'):
        job.step(store.conn)
    failed = job.progress(store.conn)
    regular.migrate(store.conn)
    assert job.progress(store.conn) == failed
    monkeypatch.setattr(policy_documents, '_publish_encoded', publish)
    checkpoint = job.step(store.conn, batch_size=1, retry=True)
    def interrupt(*args):
        shares.copy(*args)
        raise KeyboardInterrupt('controlled album interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(job, copy=interrupt).step(store.conn, batch_size=1)
    assert job.progress(store.conn) == checkpoint
    manifest = store.conn.execute('SELECT plan_filename FROM prepared_album_share_generations').fetchone()[0]
    Path(manifest).unlink()
    with pytest.raises(FileNotFoundError):
        job.step(store.conn)
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError, match='retry explicitly'):
            job.step(reopened.conn)
        job.step(reopened.conn, retry=True)
        finish(job, reopened.conn)
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_oversized_row_and_invalid_share_do_not_replace_last_good(installed):
    store, _ = installed
    seed(store)
    for job in (rows.JOB, shares.JOB):
        job.install(store.conn)
        finish(job, store.conn)
    old = shares.head(store.conn, 'release')
    with store.conn:
        store.conn.execute("UPDATE main.album_share_snapshots SET video_ids_json='[null]' WHERE browse_id='release'")
    with pytest.raises(ValueError, match='ordered nonempty'):
        shares.JOB.step(store.conn)
    assert shares.head(store.conn, 'release')['preparation_revision'] == old['preparation_revision']
    row = store.conn.execute("SELECT id FROM main.album_tracks WHERE legacy_track_key IS NOT NULL").fetchone()[0]
    previous = store.conn.execute('SELECT source_digest FROM prepared_album_track_rows WHERE row_id=?', (row,)).fetchone()[0]
    with store.conn:
        store.conn.execute('UPDATE main.album_tracks SET legacy_track_key=? WHERE id=?', ('x' * (rows.MAX_ROW_BYTES+1), row))
    with pytest.raises(ValueError, match='byte budget'):
        rows.JOB.step(store.conn)
    assert store.conn.execute('SELECT source_digest FROM prepared_album_track_rows WHERE row_id=?', (row,)).fetchone()[0] == previous
