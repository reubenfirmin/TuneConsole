"""Finite indexed checkpoint jobs retain provenance and independent progress."""
from dataclasses import replace
import json

import pytest

from tests.core.test_predecessor_writes import installed
from tests.independent_schema_support import independent_regular_ddl
from yt_playlist.core import discovery_checkpoints, provider_frontiers
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import discovery_checkpoints as preparation
from yt_playlist.core.store import Store


def finish(job, conn):
    for _ in range(100):
        progress = job.step(conn, batch_size=128)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Discovery checkpoint fixture did not finish')


def seed(store):
    store.tracks.upsert_track('video', 'Song', 'Artist', 'Album', 100, album_browse_id='release')
    artist, = store.conn.execute("SELECT id FROM artists WHERE name='Artist'").fetchone()
    values = {
        'fetch-history': {'release': '1', 'unknown-release': '1', 'radio:episode': '1', 'artist:external': '1', 'genre:exact label': '1'},
        'artist-images': {str(artist): '123.50', '0' + str(artist): '124', '999': '125'},
        'provider-refresh': {'known': '{ "cursor": 7, "cutoff": 10, "fingerprint": "exact été" }',
                             'unknown': '{"cursor":0,"cutoff":9,"fingerprint":"other","revision":3}'},
    }
    with store.conn:
        store.conn.execute("INSERT INTO enrichment_provider_config VALUES ('known','current','ready')")
        store.conn.executemany('INSERT INTO settings VALUES (?,?)',
            ((preparation.PREFIXES[name] + key, raw) for name, rows in values.items() for key, raw in rows.items()))
    return values


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
@pytest.mark.parametrize('name', list(preparation.JOBS))
def test_exact_originals_and_missing_references_remain_without_admission(installed, name):
    store, _ = installed
    conn = store.conn
    values = seed(store)
    before = {table: conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
              for table in ('album_provider_ids', 'artists', 'enrichment_provider_config')}
    job = preparation.JOBS[name]
    job.install(conn)
    finish(job, conn)
    assert {row[0]: row[1] for row in conn.execute(f'SELECT source,raw_value FROM {preparation.originals(name)}')} == {
        preparation.PREFIXES[name] + key: value for key, value in values[name].items()}
    for key, value in values[name].items():
        assert store.settings.get_setting(preparation.PREFIXES[name] + key) == value
    if name == 'fetch-history':
        assert [tuple(row) for row in conn.execute(f'SELECT source_type,source_id,album_id IS NOT NULL,unresolved_reason '
            f'FROM {preparation.TABLES[name]} ORDER BY source')] == [
                ('artist', 'external', 0, None), ('genre', 'exact label', 0, None), ('radio', 'episode', 0, None),
                ('album', 'release', 1, None), ('album', 'unknown-release', 0, 'missing_album')]
    elif name == 'artist-images':
        assert [tuple(row) for row in conn.execute(f'SELECT raw_artist_id,artist_id IS NOT NULL,unresolved_artist_id,retry_at '
            f'FROM {preparation.TABLES[name]} ORDER BY source')] == [('01', 1, None, 124), ('1', 1, None, 123.5), ('999', 0, 999, 125)]
    else:
        assert [tuple(row) for row in conn.execute(f'SELECT raw_provider,provider,unresolved_reason,cursor,cutoff,fingerprint,revision '
            f'FROM {preparation.TABLES[name]} ORDER BY source')] == [
                ('known', 'known', None, 7, 10, 'exact été', 1), ('unknown', None, 'missing_provider', 0, 9, 'other', 3)]
    assert before == {table: conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in before}
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_separate_frozen_upgrade_preserves_supported_checkpoint_values(installed, tmp_path):
    store, _ = installed
    seed(store)
    with store.conn:
        for name, suffix in [('fetch-history', 'unknown-release'), ('artist-images', '01'), ('artist-images', '999'), ('provider-refresh', 'unknown')]:
            store.conn.execute('DELETE FROM settings WHERE key=?', (preparation.PREFIXES[name] + suffix,))
    for job in preparation.JOBS.values():
        job.install(store.conn)
        finish(job, store.conn)
    upgraded = Store(tmp_path / 'checkpoint-upgrade.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS[:94])
        for name, native, columns in [
            ('fetch-history', 'discovery_fetch_history', 'source_type,source_id,album_id'),
            ('artist-images', 'discovery_artist_image_state', 'artist_id,retry_at'),
            ('provider-refresh', 'provider_refresh_frontiers', 'provider,cursor,cutoff,fingerprint,revision'),
        ]:
            assert [tuple(row) for row in upgraded.conn.execute(f'SELECT {columns} FROM {native} ORDER BY 1,2')] == [
                tuple(row) for row in store.conn.execute(f'SELECT {columns} FROM {preparation.TABLES[name]} ORDER BY 1,2')]
    finally:
        upgraded.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('name', list(preparation.JOBS))
def test_many_checkpoints_commit_at_most_sixteen_rows_and_use_prefix_index(installed, name):
    store, _ = installed
    prefix, job = preparation.PREFIXES[name], preparation.JOBS[name]
    raw = {'fetch-history': '1', 'artist-images': '123',
           'provider-refresh': '{"cursor":0,"cutoff":1,"fingerprint":"f"}'}[name]
    with store.conn:
        store.conn.executemany('INSERT INTO settings VALUES (?,?)', ((prefix + str(i + 1), raw) for i in range(100)))
        store.conn.executemany('INSERT INTO settings VALUES (?,?)', ((f'unrelated:{i}', 'opaque') for i in range(100)))
    job.install(store.conn)
    prior = 0
    for _ in range(10):
        progress = job.step(store.conn, batch_size=512)
        assert 0 <= progress.processed - prior <= 16
        prior = progress.processed
        if progress.status == 'prepared':
            break
    else:
        pytest.fail('Bounded checkpoints did not finish')
    assert store.conn.execute(f'SELECT count(*) FROM {preparation.TABLES[name]}').fetchone()[0] == 100
    query = 'SELECT key,value FROM main.settings WHERE key<=? AND (' + job._filter('settings') + ') ORDER BY key LIMIT 16'
    plan = store.conn.execute('EXPLAIN QUERY PLAN ' + query, (prefix + '999',)).fetchall()
    assert any('SEARCH main.settings' in row[3] and 'key>?' in row[3] and 'key<?' in row[3] for row in plan)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_old_writers_reference_updates_and_cross_prefix_rename_reconcile(installed):
    store, _ = installed
    conn = store.conn
    seed(store)
    for job in preparation.JOBS.values():
        job.install(conn)
        finish(job, conn)
    with conn:
        provider_frontiers.start(conn, 'known', 20, 'new')
        discovery_checkpoints.remember(conn, 'later-release')
    for name in ('provider-refresh', 'fetch-history'):
        assert preparation.JOBS[name].progress(conn).prepared_revision is None
        finish(preparation.JOBS[name], conn)
    assert tuple(conn.execute(f'SELECT cursor,cutoff,fingerprint FROM {preparation.TABLES["provider-refresh"]} '
                             "WHERE raw_provider='known'").fetchone()) == (0, 20, 'new')
    assert conn.execute(f'SELECT count(*) FROM {preparation.originals("provider-refresh")} '
                        'WHERE source=?', (provider_frontiers.PREFIX + 'known',)).fetchone()[0] == 2
    store.tracks.upsert_track('new-video', 'New', 'Artist', 'New Album', 100, album_browse_id='unknown-release')
    finish(preparation.JOBS['fetch-history'], conn)
    assert conn.execute(f'SELECT unresolved_reason FROM {preparation.TABLES["fetch-history"]} '
                        "WHERE source_id='unknown-release'").fetchone()[0] is None
    with conn:
        conn.execute("INSERT INTO enrichment_provider_config VALUES ('unknown','current','ready')")
    finish(preparation.JOBS['provider-refresh'], conn)
    assert conn.execute(f'SELECT provider FROM {preparation.TABLES["provider-refresh"]} '
                        "WHERE raw_provider='unknown'").fetchone()[0] == 'unknown'
    old = conn.execute(f'SELECT * FROM {preparation.TABLES["artist-images"]} WHERE raw_artist_id=\'1\'').fetchone()
    with conn:
        conn.execute('DELETE FROM settings WHERE key=?', (preparation.PREFIXES['artist-images'] + '1',))
        conn.execute('UPDATE settings SET key=? WHERE key=?',
                     (preparation.PREFIXES['artist-images'] + '1', provider_frontiers.PREFIX + 'known'))
    finish(preparation.JOBS['provider-refresh'], conn)
    assert not conn.execute(f'SELECT 1 FROM {preparation.TABLES["provider-refresh"]} WHERE raw_provider=\'known\'').fetchone()
    assert tuple(conn.execute(f'SELECT * FROM {preparation.TABLES["artist-images"]} WHERE raw_artist_id=\'1\'').fetchone()) == tuple(old)
    assert preparation.JOBS['artist-images'].progress(conn).prepared_revision is None


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_interrupted_batch_and_failure_keep_independent_progress_for_reopened_retry(
        installed, independent_regular_ddl):
    store, _ = installed
    conn = store.conn
    seed(store)
    job = preparation.JOBS['fetch-history']
    other = preparation.JOBS['provider-refresh']
    job.install(conn)
    other.install(conn)
    checkpoint = job.step(conn, batch_size=1)
    def interrupted(*args):
        preparation.fetch(*args)
        raise KeyboardInterrupt('controlled checkpoint interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(job, copy=interrupted).step(conn, batch_size=1)
    assert job.progress(conn) == checkpoint
    with conn:
        conn.execute('UPDATE settings SET value=? WHERE key=?', ('bad', preparation.PREFIXES['fetch-history'] + 'release'))
    with pytest.raises(ValueError):
        finish(job, conn)
    regular.migrate(conn)
    assert finish(other, conn).status == 'prepared'
    store.settings.set_setting(preparation.PREFIXES['fetch-history'] + 'release', '1')
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError, match='retry explicitly'):
            job.step(reopened.conn)
        job.step(reopened.conn, retry=True)
        finish(job, reopened.conn)
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('name,suffix,raw', [
    ('fetch-history', 'album', 'true'), ('fetch-history', 'radio:', '1'),
    ('artist-images', 'artist-name', '123'), ('artist-images', '1', 'NaN'),
    ('provider-refresh', 'provider', '{"cursor":0,"cutoff":1,"fingerprint":"f","revision":0}'),
    ('provider-refresh', 'provider', '{"cursor":-1,"cutoff":1,"fingerprint":"f"}'),
    ('provider-refresh', 'provider', '{"cursor":0,"cutoff":1,"fingerprint":"f","extra":true}'),
    ('provider-refresh', 'provider', ' ' * (preparation.MAX_BYTES + 1)),
])
def test_invalid_checkpoints_fail_durably_and_retain_source(installed, name, suffix, raw):
    store, _ = installed
    source = preparation.PREFIXES[name] + suffix
    store.settings.set_setting(source, raw)
    job = preparation.JOBS[name]
    job.install(store.conn)
    with pytest.raises(ValueError):
        finish(job, store.conn)
    assert job.progress(store.conn).status == 'failed'
    assert store.settings.get_setting(source) == raw
