"""TV preparation retains finite typed relations and exact replay receipts."""
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from tests.store_fixture_support import (
    native_sql_calls,
    numbered_store as installed,
    preparation_ddl_connection,
    publication_preparation_input,
    without_query_timing,
)
from tests.test_tv_stream import video
from yt_playlist.core import schema_inspection, tv_recommendation_state as state
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import artist_lookup, tv_state as preparation
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Preparation tests do not construct the HTTP application."""


def finish(job, conn, size=128):
    for _ in range(300):
        progress = job.step(conn, batch_size=size)
        if progress.status == 'prepared':
            return progress
    pytest.fail('TV preparation did not finish its bounded fixture')


def seed(store):
    from tests.recommendation_support import seed_publication_subjects
    from yt_playlist.core import artists
    from yt_playlist.core.db_writes import atomic_write

    seed_publication_subjects(store, [{'key': 'song|artist', 'video_id': 'visual00001',
                                     'title': 'Song', 'artist': 'Artist', 'duration_s': 100}])
    with atomic_write(store):
        artists.observe(store.conn, store.conn.execute(
            "SELECT id FROM recordings WHERE identity_key='song|artist'").fetchone()[0], 'Artist')
    recording = store.conn.execute("SELECT identity_key FROM catalog_tracks WHERE video_id='visual00001'").fetchone()[0]
    values = {'tv_pool': {'schema': 3, 'key': 'original', 'at': 8,
        'items': [{**video(1), 'source_key': recording}, video(2)], 'attempted': [recording, 'missing|artist']},
        'tv_artist_artwork': {'ARTIST!': {'images': ['https://example.test/photo'], 'retry_at': 100},
            'unknown': {'images': [], 'at': None}},
        'tv_artist_artwork_requests': {'known-key': 'ARTIST!', 'unknown': 'Unknown'},
        'tv_visual_checks': {'visual00001': {'version': 2, 'verdict': 'static', 'sample_times': [20, 60]}},
        'tv_continuations': {'10::request:with:colons': {'playlist_id': 20, 'stream': 'next',
            'items': [{**video(1), 'key': video(1)['video_id']}]}}}
    originals = {surface: json.dumps(value, ensure_ascii=False, indent=2) for surface, value in values.items()}
    with store.conn:
        store.conn.executemany('INSERT INTO rec_proposals VALUES (?,?,?)', [(surface, raw, None) for surface, raw in originals.items()])
    return values, originals


def head(conn, surface):
    generations, current, _parts = preparation.tables(surface)
    return conn.execute(f'SELECT g.* FROM {current} h JOIN {generations} g USING(source,preparation_revision)').fetchone()


def aliases(conn):
    artist_lookup.JOB.install(conn)
    finish(artist_lookup.JOB, conn)


@pytest.mark.parametrize('installed', [79, 93, 94], indirect=True)
@pytest.mark.usefixtures('publication_preparation_input')
def test_all_five_jobs_retain_exact_shapes_clocks_and_reference_custody(installed, preparation_ddl_connection):
    store, _ = installed
    conn = preparation_ddl_connection
    values, originals = seed(store)
    aliases(conn)
    for name, job in preparation.JOBS.items():
        job.install(conn)
        finish(job, conn, size=1)
        surface = preparation.SOURCES[name]
        row = head(conn, surface)
        assert preparation.read_original(conn, row) == originals[surface]
        assert row['built_at'] is None
        assert store.rec_proposals.get_proposals(surface) == values[surface]
        for relation in preparation.RELATIONS[surface]:
            assert not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (relation,)).fetchone()
            assert all(fk[2].startswith('prepared_') for fk in conn.execute(f'PRAGMA foreign_key_list({preparation.relation(relation)})'))
    assert [tuple(row) for row in conn.execute('SELECT source_resolution,video_resolution FROM ' +
        preparation.relation('tv_pool_items') + ' ORDER BY position')] == [(None, None), ('missing_recording', 'external_video')]
    assert [tuple(row) for row in conn.execute('SELECT requested_at,resolution_reason FROM ' +
        preparation.relation('tv_artist_requests') + ' ORDER BY id')] == [(None, None), (None, 'missing_artist')]
    assert tuple(conn.execute('SELECT source_ref,target_ref,source_resolution,target_resolution,input_stream,request_id,built_at FROM ' +
        preparation.relation('tv_continuation_receipts')).fetchone()) == (None, None, 'missing_playlist', 'missing_playlist', '', 'request:with:colons', None)
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()
    regular.migrate(conn)


@pytest.mark.parametrize('installed', [79, 94], indirect=True)
@pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')
def test_frozen_v95_upgrade_matches_every_prepared_relation_and_keeps_sources_for_upgrade(installed, tmp_path):
    store, _ = installed
    values, _ = seed(store)
    aliases(store.conn)
    for job in preparation.JOBS.values():
        job.install(store.conn)
        finish(job, store.conn)
    with (closing(sqlite3.connect(tmp_path / 'tv-state-frozen-upgrade.db')) as upgraded,
          schema_inspection.reuse_tables(upgraded)):
        upgraded.row_factory = sqlite3.Row
        upgraded.execute('PRAGMA foreign_keys=ON')
        upgraded.execute('PRAGMA synchronous=OFF')
        store.conn.backup(upgraded)
        upgraded.execute('PRAGMA journal_mode=WAL')
        runner.migrate(upgraded, registry=registry.MIGRATIONS[:95])
        for surface, value in values.items():
            assert state.read(upgraded, surface) == value
            for table in preparation.RELATIONS[surface]:
                fields = ','.join('"' + row[1] + '"' for row in upgraded.execute(f'PRAGMA table_info({table})'))
                assert list(map(tuple, upgraded.execute(f'SELECT {fields} FROM {table} ORDER BY 1,2'))) == list(map(tuple,
                    store.conn.execute(f'SELECT {fields} FROM {preparation.relation(table)} ORDER BY 1,2')))


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_replacement_reuses_captured_identities_in_each_owner_without_mutating_earlier_generation(installed):
    store, _ = installed
    values, originals = seed(store)
    aliases(store.conn)
    for name, job in preparation.JOBS.items():
        surface = preparation.SOURCES[name]
        job.install(store.conn)
        finish(job, store.conn)
        previous = head(store.conn, surface)
        store.rec_proposals.put_proposals(surface, values[surface], 90)
        finish(job, store.conn)
        current = head(store.conn, surface)
        assert current['preparation_revision'] != previous['preparation_revision']
        assert current['built_at'] == 90
        assert json.loads(preparation.read_original(store.conn, current)) == values[surface]
        assert preparation.read_original(store.conn, previous) == originals[surface]
        for relation in preparation.RELATIONS[surface]:
            table = preparation.relation(relation)
            old = store.conn.execute(f'SELECT count(*) FROM {table} WHERE preparation_revision=?',
                                     (previous['preparation_revision'],)).fetchone()[0]
            new = store.conn.execute(f'SELECT count(*) FROM {table} WHERE preparation_revision=?',
                                     (current['preparation_revision'],)).fetchone()[0]
            assert new == old


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_full_frozen_upgrade_after_tv_staging_keeps_native_readers_and_original_files(installed, tmp_path):
    store, _ = installed
    values, originals = seed(store)
    aliases(store.conn)
    for job in preparation.JOBS.values():
        job.install(store.conn)
        finish(job, store.conn)
    upgraded = Store(tmp_path / 'tv-full-upgrade.db')
    try:
        store.conn.backup(upgraded.conn)
        runner.migrate(upgraded.conn, registry=registry.MIGRATIONS)
        for surface, value in values.items():
            assert state.read(upgraded.conn, surface) == value
            assert preparation.read_original(upgraded.conn, head(upgraded.conn, surface)) == originals[surface]
        assert not upgraded.conn.execute('PRAGMA foreign_key_check').fetchone()
        assert upgraded.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == len(registry.MIGRATIONS)
    finally:
        upgraded.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_continuing_alias_and_video_updates_reconcile_without_admission(installed):
    store, _ = installed
    seed(store)
    aliases(store.conn)
    for job in preparation.JOBS.values():
        job.install(store.conn)
        finish(job, store.conn)
    pool, artwork = preparation.JOBS['pool'], preparation.JOBS['artwork-requests']
    store.tracks.upsert_track('visual00002', 'Arrived', 'Unknown', None, 100)
    assert pool.progress(store.conn).prepared_revision is None
    assert artwork.progress(store.conn).prepared_revision is None
    assert artist_lookup.JOB.progress(store.conn).prepared_revision is None
    finish(artist_lookup.JOB, store.conn)
    finish(pool, store.conn)
    finish(artwork, store.conn)
    current = head(store.conn, 'tv_pool')
    assert store.conn.execute('SELECT video_resolution FROM ' + preparation.relation('tv_pool_items') +
        ' WHERE preparation_revision=? AND position=1', (current['preparation_revision'],)).fetchone()[0] is None
    current = head(store.conn, 'tv_artist_artwork_requests')
    assert store.conn.execute('SELECT resolution_reason FROM ' + preparation.relation('tv_artist_requests') +
        ' WHERE preparation_revision=? AND artist_key=\'unknown\'', (current['preparation_revision'],)).fetchone()[0] is None
    with store.conn:
        store.conn.execute("UPDATE rec_proposals SET surface='retained-tv-requests' WHERE surface='tv_artist_artwork_requests'")
    finish(artwork, store.conn)
    assert head(store.conn, 'tv_artist_artwork_requests') is None
    assert preparation.read_original(store.conn, current)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_file_work_has_no_transaction_and_rejects_concurrent_source_edit(installed, monkeypatch):
    store, _ = installed
    seed(store)
    job = preparation.JOBS['pool']
    job.install(store.conn)
    real_publish = preparation.files.publish
    changed = False
    def publish(root, encoded):
        nonlocal changed
        assert not store.conn.in_transaction
        if not changed:
            changed = True
            store.rec_proposals.put_proposals('tv_pool', {'items': [], 'state': 'no_sources'}, 20)
        return real_publish(root, encoded)
    monkeypatch.setattr(preparation.files, 'publish', publish)
    assert job.step(store.conn).input_revision == 1
    assert head(store.conn, 'tv_pool') is None
    finish(job, store.conn)
    assert json.loads(preparation.read_original(store.conn, head(store.conn, 'tv_pool'))) == {'items': [], 'state': 'no_sources'}


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.usefixtures('publication_preparation_input')
def test_interruption_missing_file_failed_job_new_regular_ddl_and_reopened_retry(installed, preparation_ddl_connection, monkeypatch):
    store, _ = installed
    seed(store)
    pool, visual = preparation.JOBS['pool'], preparation.JOBS['visual-checks']
    for job in (pool, visual):
        job.install(store.conn)
    checkpoint = pool.step(store.conn, batch_size=1)
    def interrupt(*args):
        preparation.copy(*args)
        raise KeyboardInterrupt('controlled TV row interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(pool, copy=interrupt).step(store.conn, batch_size=1)
    assert pool.progress(store.conn) == checkpoint
    real_publish = preparation.files.publish
    def disk_failure(*args):
        assert not store.conn.in_transaction
        raise OSError('controlled TV file failure')
    monkeypatch.setattr(preparation.files, 'publish', disk_failure)
    with pytest.raises(OSError, match='file failure'):
        pool.step(store.conn)
    failed = pool.progress(store.conn)
    monkeypatch.setattr(preparation.files, 'publish', real_publish)
    finish(visual, store.conn)
    def install(conn, context):
        conn.execute('CREATE TABLE independent_tv_ddl(value TEXT)')
    def validate(conn):
        assert conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='independent_tv_ddl'").fetchone()
    monkeypatch.setattr(regular, 'CHANGES', (*regular.CHANGES, regular.Change(len(regular.CHANGES) + 1,
        'independent_tv_ddl', install, validate)))
    regular.migrate(preparation_ddl_connection)
    assert store.conn.execute('SELECT max(version) FROM schema_changes').fetchone()[0] == len(regular.CHANGES)
    assert pool.progress(store.conn) == failed
    reopened = Store(store._db_path)
    try:
        with pytest.raises(MigrationError, match='retry explicitly'):
            pool.step(reopened.conn)
        pool.step(reopened.conn, retry=True)
        finish(pool, reopened.conn)
        row = head(reopened.conn, 'tv_pool')
        part = reopened.conn.execute('SELECT filename FROM ' + preparation.tables('tv_pool')[2] + ' LIMIT 1').fetchone()[0]
        Path(part).unlink()
        with pytest.raises(OSError):
            preparation.read_original(reopened.conn, row)
        assert store.rec_proposals.get_proposals('tv_pool')['key'] == 'original'
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_alias_preparation_is_bounded_handles_ambiguity_and_is_an_explicit_artwork_dependency(installed):
    store, _ = installed
    seed(store)
    job = preparation.JOBS['artwork-requests']
    job.install(store.conn)
    with pytest.raises(MigrationError, match='aliases job first'):
        job.step(store.conn)
    artist = store.conn.execute("SELECT artist_id FROM artist_aliases WHERE alias='Artist'").fetchone()[0]
    with store.conn:
        store.conn.executemany('INSERT INTO artist_aliases VALUES (?,?,?)',
            [(artist, 'Artist' + '!' * number, 'controlled') for number in range(1, 1001)])
    for _ in range(40):
        before = store.conn.execute('SELECT count(*) FROM ' + artist_lookup.ALIASES).fetchone()[0]
        progress = artist_lookup.JOB.step(store.conn, batch_size=128)
        after = store.conn.execute('SELECT count(*) FROM ' + artist_lookup.ALIASES).fetchone()[0]
        assert after - before <= 32
        if progress.status == 'prepared':
            break
    assert progress.status == 'prepared'
    assert artist_lookup.lookup(store.conn, 'ÁRTIST?') == artist
    assert store.conn.execute(f"SELECT support_count FROM {artist_lookup.MATCHES} WHERE kind='normalized' AND key='artist'").fetchone()[0] == 1001
    plan = store.conn.execute(f"EXPLAIN QUERY PLAN SELECT canonical_artist FROM {artist_lookup.MATCHES} WHERE kind='normalized' AND key=? ORDER BY canonical_artist LIMIT 2", ('artist',)).fetchall()
    assert all('SCAN' not in row[-1] for row in plan)
    job.step(store.conn, retry=True)
    finish(job, store.conn)
    store.tracks.upsert_track('other-video', 'Other', 'Other Artist', None, 100)
    other = store.conn.execute("SELECT artist_id FROM artist_aliases WHERE alias='Other Artist'").fetchone()[0]
    with store.conn:
        store.conn.execute('INSERT INTO artist_aliases VALUES (?,?,?)', (other, 'Ártist', 'controlled'))
    finish(artist_lookup.JOB, store.conn)
    assert artist_lookup.lookup(store.conn, 'ARTIST?') is None
    assert artist_lookup.lookup(store.conn, 'Artist!') == artist  # Exact aliases precede normalized ambiguity.
    with store.conn:
        store.conn.execute('DELETE FROM artist_aliases WHERE artist_id=? AND source=?', (other, 'controlled'))
    finish(artist_lookup.JOB, store.conn)
    assert artist_lookup.lookup(store.conn, 'ARTIST?') == artist


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('surface,raw', [
    ('tv_pool', '{"items":[{"video_id":"bad"}]}'), ('tv_pool', '{"items":[],"extra":1}'),
    ('tv_pool', '{"attempted":["duplicate","duplicate"]}'),
    ('tv_artist_artwork', '{"artist":{"images":["same","same"]}}'),
    ('tv_visual_checks', '{"visual00001":{"version":2,"changed_fraction":2}}'),
    ('tv_visual_checks', '{"visual00001":{"sample_times":[20,60,90]}}'),
    ('tv_continuations', '{"10::":{"playlist_id":20,"stream":"next"}}'),
    ('tv_continuations', '{"10::request":{"playlist_id":true,"stream":"next"}}'),
    ('tv_pool', 'null'), ('tv_pool', ' ' * (state.MAX_BYTES + 1)),
])
def test_invalid_sources_fail_durably_without_changing_original_or_selecting_partial_state(installed, preparation_ddl_connection, surface, raw):
    conn = preparation_ddl_connection
    with conn:
        conn.execute('INSERT INTO rec_proposals VALUES (?,?,NULL)', (surface, raw))
    aliases(conn)
    name = next(name for name, source in preparation.SOURCES.items() if source == surface)
    job = preparation.JOBS[name]
    job.install(conn)
    with pytest.raises((ValueError, MigrationError, sqlite3.IntegrityError)):
        finish(job, conn)
    assert job.progress(conn).status == 'failed'
    assert head(conn, surface) is None
    assert conn.execute('SELECT payload FROM rec_proposals WHERE surface=?', (surface,)).fetchone()[0] == raw
    regular.migrate(conn)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_full_receipt_budget_copies_individual_items_and_keeps_prior_complete_generation(installed):
    store, _ = installed
    job = preparation.JOBS['continuations']
    store.rec_proposals.put_proposals('tv_continuations', {}, None)
    job.install(store.conn)
    finish(job, store.conn)
    previous = head(store.conn, 'tv_continuations')
    values = {f'{number + 1}::request:{number}': {'playlist_id': number + 101, 'stream': f'stream-{number}',
        'items': [video(index) for index in range(60)]} for number in range(60)}
    raw = json.dumps(values, indent=2)
    store.rec_proposals.put_proposals('tv_continuations', values, 30)
    tables = [preparation.relation(name) for name in preparation.RELATIONS['tv_continuations']] + [preparation.tables('tv_continuations')[2]]
    for _ in range(40):
        before = sum(store.conn.execute('SELECT count(*) FROM ' + table).fetchone()[0] for table in tables)
        progress = job.step(store.conn, batch_size=128)
        after = sum(store.conn.execute('SELECT count(*) FROM ' + table).fetchone()[0] for table in tables)
        assert 0 <= after - before <= 128
        if progress.status == 'prepared':
            break
        assert head(store.conn, 'tv_continuations')['preparation_revision'] == previous['preparation_revision']
    assert progress.status == 'prepared'
    current = head(store.conn, 'tv_continuations')
    assert current['row_count'] == 3660
    assert current['part_count'] > 1
    assert json.loads(preparation.read_original(store.conn, current)) == json.loads(raw)
    assert preparation.read_original(store.conn, previous) == '{}'
    assert store.conn.execute('SELECT count(*) FROM ' + preparation.relation('tv_continuation_items')).fetchone()[0] == 3600
