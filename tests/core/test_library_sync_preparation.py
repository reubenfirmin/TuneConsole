"""Retained sync reports checkpoint actual resources without guessing owners."""
from dataclasses import replace
import json

import pytest

from tests.store_fixture_support import numbered_store as installed, preparation_ddl_connection, publication_preparation_input
from yt_playlist.core import entity_names, recommendation_outputs as files, sync_state
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import library_sync as preparation
from yt_playlist.core.store import Store
from yt_playlist.library import sync_outcomes


pytestmark = pytest.mark.usefixtures('publication_preparation_input')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Sync-state preparation does not construct an HTTP app."""


def finish(job, conn, size=16):
    for _ in range(100):
        progress = job.step(conn, batch_size=size)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Library sync fixture did not finish')


def head(conn):
    return conn.execute(f'SELECT g.* FROM {preparation.CURRENT} h JOIN {preparation.HEAD} g USING(source,generation)').fetchone()


def seed(store):
    first = store.accounts.upsert_account('First', 'first.json', None, True)
    store.playlists.upsert_playlist(first, 'LM', 'Liked Music', 0, '', 1)
    value = {'status': 'partial', 'last_attempt_at': 20, 'accounts': {
        str(first): {'status': 'partial', 'label': 'Exact label', 'resources': {
            'playlists': {'status': 'partial', 'cursor': 'exact cursor', 'last_attempt_at': 20, 'items': {
                'LM': {'status': 'complete', 'last_success_at': 18},
                'missing-playlist': {'status': 'failed', 'message': 'offline', 'failures': 2, 'retry_at': 90}}},
            'history': {'status': 'failed', 'last_attempt_at': 20, 'failures': 1, 'retry_at': 50}}},
        '999': {'status': 'failed', 'resources': {'ratings': {'status': 'skipped'}}}},
        'resources': {'album_tracks': {'status': 'complete', 'cursor': ''}}}
    raw = json.dumps(value, ensure_ascii=False, indent=2)
    with store.conn:
        store.conn.executemany('INSERT INTO settings VALUES (?,?)', [
            ('library_sync_state', raw), ('last_sync_at', ' 10.50 '), ('last_sync_attempt_at', '20'), ('last_sync_outcome', 'partial')])
    return raw


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
def test_exact_report_resources_stamps_and_unresolved_owners_remain_retained(installed, preparation_ddl_connection):
    store, _ = installed
    conn = preparation_ddl_connection
    raw = seed(store)
    scopes_before = conn.execute('SELECT count(*) FROM main.playlist_scopes').fetchone()[0]
    for job in (preparation.RESULT_JOB, preparation.STAMP_JOB):
        job.install(conn)
        finish(job, conn)
    assert preparation.read_original(conn, head(conn)) == raw
    assert sync_state.read(conn) == json.loads(raw)
    assert head(conn)['phase'] == 'partial'
    rows = [tuple(row) for row in conn.execute(f'SELECT raw_account_id,account_id,unresolved_account_id,collection,'
        f'raw_playlist_id,playlist_scope_id,unresolved_scope_reason FROM {preparation.CURSORS} ORDER BY position')]
    assert rows == [(str(1), 1, None, 'account', None, None, None),
                    ('1', 1, None, 'playlists', None, None, None),
                    ('1', 1, None, 'playlist', 'LM', '@account:1:LM', None),
                    ('1', 1, None, 'playlist', 'missing-playlist', None, 'missing_scope'),
                    ('1', 1, None, 'history', None, None, None),
                    ('999', None, 999, 'account', None, None, None),
                    ('999', None, 999, 'ratings', None, None, None),
                    (None, None, None, 'album_tracks', None, None, None)]
    assert [tuple(row) for row in conn.execute(f'SELECT source,timestamp,outcome FROM {preparation.STAMPS} ORDER BY source')] == [
        ('last_sync_at', 10.5, None), ('last_sync_attempt_at', 20, None), ('last_sync_outcome', None, 'partial')]
    assert conn.execute('SELECT count(*) FROM main.playlist_scopes').fetchone()[0] == scopes_before
    assert not sync_state.available(conn)
    assert files.collect_orphans(conn) == 0
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()
    regular.migrate(conn)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_separate_frozen_upgrade_preserves_supported_cursor_and_stamp_values(installed, tmp_path):
    store, _ = installed
    value = json.loads(seed(store))
    del value['accounts']['999']
    del value['accounts']['1']['resources']['playlists']['items']['missing-playlist']
    store.settings.set_setting('library_sync_state', json.dumps(value))
    for job in (preparation.RESULT_JOB, preparation.STAMP_JOB):
        job.install(store.conn)
        finish(job, store.conn)
    upgraded = Store(tmp_path / 'sync-upgrade.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS[:94])
        columns = 'account_id,collection,playlist_scope_id,cursor,phase,updated_at,last_success_at,failures,retry_at'
        assert [tuple(row) for row in upgraded.conn.execute(f'SELECT {columns} FROM sync_cursors ORDER BY id')] == [
            tuple(row) for row in store.conn.execute(f'SELECT {columns} FROM {preparation.CURSORS} ORDER BY position')]
        assert sync_state.read(upgraded.conn) == value
        assert [sync_state.stamp(upgraded.conn, key) for key in ('last_sync_at','last_sync_attempt_at','last_sync_outcome')] == ['10.5', '20.0', 'partial']
    finally:
        upgraded.conn.close()


@pytest.mark.parametrize('installed', [94, 102, 107], indirect=True)
def test_typed_sync_reader_uses_physical_scopes_without_temp_views(installed):
    store, _ = installed
    conn = store.conn
    account = store.accounts.upsert_account('First', 'first.json', None, True)
    entity_names.clear_predecessor_reads(conn)
    result = {'status': 'complete', 'last_attempt_at': 2, 'accounts': {str(account): {
        'status': 'complete', 'resources': {'playlists': {'status': 'complete', 'items': {'LM': {'status': 'complete'}}}}}}}
    with conn:
        sync_state.write(conn, result, 2)
    assert sync_state.read(conn) == result


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('raw,kind', [(None, 'null'), ('', 'blank'), ('null', 'empty'), ('{}', 'empty')])
def test_null_blank_and_empty_sync_sources_are_distinct(installed, raw, kind):
    store, _ = installed
    with store.conn:
        store.conn.execute('INSERT INTO settings VALUES (?,?)', ('library_sync_state', raw))
    preparation.RESULT_JOB.install(store.conn)
    finish(preparation.RESULT_JOB, store.conn)
    assert preparation.read_original(store.conn, head(store.conn)) == raw
    assert head(store.conn)['source_kind'] == kind
    assert head(store.conn)['item_count'] == 0
    assert head(store.conn)['finished_at'] is None


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_large_report_checkpoints_individual_resources(installed):
    store, _ = installed
    value = json.loads(seed(store))
    value['accounts']['1']['resources']['playlists']['items'] = {
        f'playlist-{number}': {'status': 'complete'} for number in range(1200)}
    store.settings.set_setting('library_sync_state', json.dumps(value))
    job = preparation.RESULT_JOB
    job.install(store.conn)
    prior = 0
    for _ in range(15):
        progress = job.step(store.conn, batch_size=128)
        assert 0 <= progress.processed - prior <= 128
        prior = progress.processed
        if progress.status == 'prepared':
            break
    else:
        pytest.fail('Long sync report did not finish')
    assert head(store.conn)['item_count'] == 1206


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_continuing_writer_reference_edits_and_removal_preserve_prior_generation(installed):
    store, _ = installed
    conn = store.conn
    raw = seed(store)
    job = preparation.RESULT_JOB
    job.install(conn)
    finish(job, conn)
    previous = head(conn)
    sync_outcomes.begin_attempt(store, 25)
    sync_outcomes.finish_attempt(store, json.loads(raw), 25)
    assert store.settings.get_setting('last_sync_attempt_at') == '25'
    finish(job, conn)
    assert json.loads(preparation.read_original(conn, head(conn)))['last_attempt_at'] == 25
    assert preparation.read_original(conn, previous) == raw
    store.playlists.upsert_playlist(1, 'missing-playlist', 'Now known', 0, '', 1)
    assert job.progress(conn).prepared_revision is None
    finish(job, conn)
    assert conn.execute(f'SELECT playlist_scope_id FROM {preparation.CURSORS} WHERE generation=? '
        'AND raw_playlist_id=?', (head(conn)['generation'], 'missing-playlist')).fetchone()[0] == 'missing-playlist'
    with conn:
        conn.execute("UPDATE settings SET key='retained-unrelated' WHERE key='library_sync_state'")
    finish(job, conn)
    assert head(conn) is None
    assert preparation.read_original(conn, previous) == raw


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_file_work_allows_concurrent_source_update_and_rejects_stale_copy(installed, monkeypatch):
    store, _ = installed
    conn = store.conn
    seed(store)
    job = preparation.RESULT_JOB
    job.install(conn)
    second = Store(store._db_path)
    publish = files.publish
    calls = []
    try:
        def edit(*args):
            assert not conn.in_transaction
            if not calls:
                with second.conn:
                    second.conn.execute("UPDATE settings SET value='{}' WHERE key='library_sync_state'")
                calls.append(True)
            return publish(*args)
        monkeypatch.setattr(files, 'publish', edit)
        assert job.step(conn, batch_size=1).input_revision == 1
        assert head(conn) is None
        finish(job, conn)
        assert preparation.read_original(conn, head(conn)) == '{}'
    finally:
        second.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_interrupted_head_and_disk_failure_keep_independent_stamps_and_reopened_retry(
        installed, preparation_ddl_connection, monkeypatch):
    store, _ = installed
    conn = store.conn
    raw = seed(store)
    job = preparation.RESULT_JOB
    job.install(conn)
    preparation.STAMP_JOB.install(conn)
    checkpoint = job.step(conn, batch_size=1)
    def interrupted(*args):
        preparation.complete(*args)
        raise KeyboardInterrupt('controlled sync head interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(job, complete=interrupted).step(conn)
    assert job.progress(conn) == checkpoint
    assert head(conn) is None
    assert not conn.execute(f'SELECT 1 FROM {preparation.CURSORS}').fetchone()
    # A new input generation needs a file write; resuming the committed file
    # checkpoint above reuses its manifest without writing the file again.
    with conn:
        conn.execute('UPDATE settings SET value=value WHERE key=?', (sync_state.KEY,))
    publish = files.publish
    def failed(*args):
        raise OSError('controlled sync disk failure')
    monkeypatch.setattr(files, 'publish', failed)
    with pytest.raises(OSError, match='controlled sync disk failure'):
        job.step(conn)
    regular.migrate(preparation_ddl_connection)
    assert finish(preparation.STAMP_JOB, conn).status == 'prepared'
    monkeypatch.setattr(files, 'publish', publish)
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError, match='retry explicitly'):
            job.step(reopened.conn)
        job.step(reopened.conn, retry=True)
        finish(job, reopened.conn)
        assert preparation.read_original(reopened.conn, head(reopened.conn)) == raw
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('source,raw', [
    ('library_sync_state', '[]'), ('library_sync_state', '{"status":"running"}'),
    ('library_sync_state', '{"status":"failed","accounts":{"01":{"status":"failed"}}}'),
    ('library_sync_state', '{"status":"failed","accounts":{"1":{"status":"failed","resources":{"other":{"status":"failed"}}}}}'),
    ('library_sync_state', '{"status":"failed","resources":{"album_tracks":{"status":"failed","failures":17}}}'),
    ('last_sync_at', 'Infinity'), ('last_sync_outcome', 'running'),
])
def test_invalid_sync_input_fails_durably_and_retains_authoritative_source(
        installed, preparation_ddl_connection, source, raw):
    store, _ = installed
    conn = preparation_ddl_connection
    store.settings.set_setting(source, raw)
    job = preparation.RESULT_JOB if source == 'library_sync_state' else preparation.STAMP_JOB
    job.install(conn)
    with pytest.raises(ValueError):
        finish(job, conn)
    assert job.progress(conn).status == 'failed'
    assert store.settings.get_setting(source) == raw
    regular.migrate(conn)
