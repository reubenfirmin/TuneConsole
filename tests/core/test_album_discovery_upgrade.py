"""One real upgrade keeps album occurrences and discovery history in custody."""
from contextlib import closing
import json
import sqlite3
from unittest.mock import Mock

import pytest

from tests.core.test_catalog import complete_regular_input
from tests.catalog_migration_support import catalog_reader
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import install_numbered_input
from yt_playlist.core import albums, artists, schema_definition
from yt_playlist.core.migrations import Migration, MigrationError, current, registry, regular, runner
from yt_playlist.core.migrations import v101_discovery_subjects, v103_entity_names
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL/file owners do not construct an HTTP lifespan."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('discovery-upgrade-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        bound_catalog(patch)
        yield


@pytest.fixture(scope='module')
def predecessor(tmp_path_factory):
    path = tmp_path_factory.mktemp('discovery-subject-upgrade') / 'old.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        install_numbered_input(conn, 98)
        yield {'conn': conn, 'path': path,
               'history': tuple(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))}


def test_predecessor_is_the_real_supported_v98_contract(predecessor):
    runner.validate_version(predecessor['conn'], 98)
    assert predecessor['conn'].execute('PRAGMA foreign_keys').fetchone()[0] == 1


@pytest.fixture(scope='module')
def occurrence_input(predecessor):
    with catalog_reader(predecessor['path'], 98) as store:
        entry = store.tracks.upsert_track('one', 'One', 'Artist', None, 10, album_browse_id='release')
        aid = albums.lookup(store.conn, 'release')
        with store.conn:
            store.conn.executemany('INSERT INTO album_tracks(album_id,entry_id,position,is_full,source) '
                "VALUES (?,?,NULL,1,'legacy')", [(aid, entry), (aid, entry)])
            store.conn.execute('INSERT INTO album_tracks(album_id,legacy_track_key,is_full,source) VALUES (?,?,1,?)',
                (aid, 'unknown|artist', 'legacy'))
            store.conn.execute('INSERT INTO album_fetch_state VALUES (?,NULL,1,3)', (aid,))
            store.conn.execute('INSERT INTO album_share_snapshots VALUES (?,?,?)',
                ('release', 'Frozen', json.dumps(['one', 'missing', 'one'])))
        predecessor['album'] = aid
        predecessor['album_rows'] = {r['id']: dict(r) for r in store.conn.execute('SELECT * FROM album_tracks')}
    return predecessor


def test_original_album_keeps_incidental_and_three_unordered_full_rows_and_exact_frozen_share(occurrence_input):
    conn = occurrence_input['conn']
    assert len(occurrence_input['album_rows']) == 4
    assert sum(row['is_full'] for row in occurrence_input['album_rows'].values()) == 3
    assert tuple(conn.execute('SELECT * FROM album_share_snapshots').fetchone()) == (
        'release', 'Frozen', json.dumps(['one', 'missing', 'one']))


def assert_occurrences(state):
    conn = state['conn']
    assert tuple(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))[:len(state['history'])] == state['history']
    assert conn.execute('SELECT current_tracklist_id FROM albums WHERE id=?', (state['album'],)).fetchone()[0] is not None
    full = conn.execute("SELECT * FROM album_tracklists WHERE capture_kind='legacy_full'").fetchone()
    assert (full['status'], full['order_status'], full['fetched_at']) == ('complete', 'unknown', None)
    captured = list(conn.execute('SELECT * FROM album_track_occurrences WHERE tracklist_id=?', (full['id'],)))
    assert len(captured) == 3 and all(row['position'] is None for row in captured)
    assert {row['legacy_row_id'] for row in captured} == {
        identity for identity, row in state['album_rows'].items() if row['is_full']}
    assert conn.execute('SELECT count(*) FROM album_item_observations').fetchone()[0] == 1
    assert tuple(conn.execute('SELECT t.capture_kind,t.status,t.order_status FROM album_share_snapshots s '
        'JOIN album_tracklists t ON t.id=s.tracklist_id').fetchone()) == ('legacy_share', 'snapshot', 'known')
    assert [row[0] for row in conn.execute('SELECT raw_source_ref FROM album_share_items ORDER BY position')] == ['one', 'missing', 'one']
    missing = conn.execute("SELECT * FROM album_track_occurrences WHERE raw_source_ref='missing'").fetchone()
    assert missing['recording_id'] is None and missing['resolution_status'] == 'unresolved'
    assert not conn.execute("SELECT 1 FROM youtube_items WHERE video_id='missing'").fetchone()
    assert conn.execute('SELECT count(*) FROM catalog_entries').fetchone()[0] == 1
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('target', [99, 100])
def test_real_occurrence_conversion_preserves_unknown_order_incidental_evidence_and_frozen_repetitions(occurrence_input, target):
    runner.migrate(occurrence_input['conn'], registry=registry.MIGRATIONS[:target], clock=lambda: 2.)
    assert_occurrences(occurrence_input)


def test_discovery_predecessor_is_the_real_v100_contract_after_the_actual_album_conversion(occurrence_input):
    runner.validate_version(occurrence_input['conn'], 100)
    assert_occurrences(occurrence_input)


@pytest.fixture(scope='module')
def populated(occurrence_input):
    conn = occurrence_input['conn']
    with conn:
        aid = artists.ensure(conn, 'Candidate')
        conn.execute('INSERT INTO artist_scans VALUES (?,NULL,?)', ('@artist:' + str(aid), aid))
        conn.execute('INSERT INTO discovered_artists(artist,artist_id,because,fits,found_at,offered_count,'
            'first_shown,last_shown,legacy_offered_count,legacy_first_shown,legacy_last_shown) '
            "VALUES (?,?,'[\"Anchor\"]','[]',1,2,3,5,8,0,2)", ('@artist:' + str(aid), aid))
        conn.execute("INSERT INTO discovery_artist_sources VALUES (?,'unknown',NULL,1)", (aid,))
        conn.execute("INSERT INTO discovery_visible_impressions VALUES ('visit','artist',?,3)", (str(aid),))
        conn.execute("INSERT INTO discovery_visible_impressions VALUES ('missing','album','absent',4)")
    occurrence_input['history'] = tuple(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    occurrence_input['original'] = list(conn.iterdump())
    return occurrence_input


def test_source_keeps_unknown_scan_original_offers_and_both_visible_receipts(populated):
    conn = populated['conn']
    assert tuple(conn.execute('SELECT offered_count,first_shown,last_shown,legacy_offered_count,'
        'legacy_first_shown,legacy_last_shown FROM discovered_artists').fetchone()) == (2, 3, 5, 8, 0, 2)
    assert conn.execute('SELECT count(*) FROM discovery_visible_impressions').fetchone()[0] == 2
    assert conn.execute('SELECT scanned_at FROM artist_scans').fetchone()[0] is None


def test_real_interrupted_cutover_rolls_back_the_exact_entire_sql_database(populated):
    def interrupted(conn, context):
        v101_discovery_subjects.apply(conn, context)
        raise RuntimeError('interrupted discovery cutover')

    with pytest.raises(MigrationError, match='interrupted discovery cutover'):
        runner.migrate(populated['conn'], registry=(*registry.MIGRATIONS[:100],
            Migration(101, 'discovery_subjects', interrupted, rebuild_tables=True)))
    assert list(populated['conn'].iterdump()) == populated['original']
    assert not populated['conn'].in_transaction
    assert not populated['conn'].execute('PRAGMA foreign_key_check').fetchone()


def assert_subjects(state):
    conn = state['conn']
    assert tuple(conn.execute('SELECT offered_count,first_shown,legacy_offered_count '
                             'FROM discovered_artists').fetchone()) == (1, 3, 9)
    assert tuple(conn.execute('SELECT original_key,reason FROM unresolved_discovery_impressions').fetchone()) == (
        'absent', 'missing_owner')
    assert tuple(conn.execute('SELECT kind,legacy_source_key,resolution_status FROM discovery_origins').fetchone()) == (
        'legacy_unknown', 'unknown', 'unresolved')
    assert conn.execute('SELECT count(*) FROM artist_scans WHERE scanned_at IS NULL').fetchone()[0] == 1
    assert tuple(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))[:100] == state['history']
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()
    assert_occurrences(state)


@pytest.mark.parametrize('target', [101, 102])
def test_real_registered_retry_and_later_owner_keep_unknowns_and_exact_offer_counts(populated, target):
    runner.migrate(populated['conn'], registry=registry.MIGRATIONS[:target], clock=lambda: 2.)
    assert_subjects(populated)


def test_account_cutover_reference_keeps_the_registered_discovery_owners():
    definitions = '\n'.join(schema_definition.statements(version=103))
    assert 'CREATE TABLE accounts' in definitions and 'discovery_origins' in definitions


@pytest.fixture(scope='module')
def renamed(populated):
    restore, guards = Mock(), Mock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(v103_entity_names, 'restore_programs', restore)
        patch.setattr(regular, 'after_owner_upgrade', guards)
        runner.migrate(populated['conn'], registry=registry.MIGRATIONS[:103], clock=lambda: 2.)
    restore.assert_called_once()
    guards.assert_called_once_with(populated['conn'])
    populated['programs'] = restore.call_args
    return populated


def test_real_account_rename_keeps_the_same_typed_discovery_evidence(renamed):
    assert renamed['conn'].execute("SELECT 1 FROM sqlite_schema WHERE name='accounts'").fetchone()
    assert_subjects(renamed)


@pytest.fixture(scope='module')
def restored(renamed):
    conn, captured = renamed['conn'], renamed['programs']
    with conn:
        conn.execute('BEGIN')
        v103_entity_names.restore_programs(*captured.args, **captured.kwargs)
        regular.after_owner_upgrade(conn)
    return renamed


def test_real_program_restoration_keeps_current_discovery_constraints(restored):
    runner.validate_version(restored['conn'], 103, foreign_keys=False, data=False)
    assert_subjects(restored)


@pytest.mark.parametrize('target', range(104, len(registry.MIGRATIONS) + 1))
def test_later_registered_owners_keep_original_offer_precision_and_applied_history(restored, target):
    runner.migrate(restored['conn'], registry=registry.MIGRATIONS[:target], clock=lambda: 2.)
    assert_subjects(restored)


@pytest.fixture(scope='module')
def regular_input(restored):
    complete_regular_input(restored['conn'])
    return restored


def test_real_current_admission_leaves_the_exact_converted_sql_unchanged(regular_input):
    before = list(regular_input['conn'].iterdump())
    assert runner.migrate(regular_input['conn']) is False
    assert list(regular_input['conn'].iterdump()) == before
    assert_subjects(regular_input)


@pytest.fixture(scope='module')
def writable(regular_input):
    store = Store(regular_input['path'])
    try:
        store.init_schema()
        yield regular_input, store
    finally:
        store.conn.close()


def test_actual_current_reader_keeps_the_original_file_backed_explanation(writable):
    state, store = writable
    candidate = store.discovery.get_discovered_artists()[0]
    assert candidate['because'] == ['Anchor']
    assert candidate['offered_count'] == 1 and candidate['first_shown'] == 3
    assert store.collection.cached_album_share('release') == {'title': 'Frozen', 'video_ids': ['one', 'missing', 'one']}
    assert_subjects(state)
    current.validate(store.conn)


def test_fresh_store_reopen_preserves_all_sql_and_the_original_explanation(writable):
    state, store = writable
    before = list(store.conn.iterdump())
    store.conn.close()
    fresh = Store(state['path'])
    try:
        fresh.init_schema()
        assert list(fresh.conn.iterdump()) == before
        assert fresh.discovery.get_discovered_artists()[0]['because'] == ['Anchor']
        assert fresh.collection.cached_album_share('release') == {'title': 'Frozen', 'video_ids': ['one', 'missing', 'one']}
        assert fresh.conn.execute('SELECT count(*) FROM album_track_occurrences').fetchone()[0] == 6
        current.validate(fresh.conn)
        assert not fresh.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        fresh.conn.close()
