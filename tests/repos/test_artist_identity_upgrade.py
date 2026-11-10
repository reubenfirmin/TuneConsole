"""One real legacy upgrade retains both first-provider and redirected handles."""
from contextlib import closing
import sqlite3
from unittest.mock import Mock

import pytest

from tests.conftest import FakeClient
from tests.catalog_migration_support import catalog_reader
from tests.core.test_catalog import complete_regular_input
from tests.genre_catalog_support import bound_catalog
from yt_playlist.core import schema_definition
from yt_playlist.core.migrations import registry, regular, runner, v027_artist_identity_redirects
from yt_playlist.core.migrations import v103_entity_names
from yt_playlist.core.store import Store
from yt_playlist.library.sync import refresh_playlist


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """No HTTP lifespan or background maintenance belongs to this upgrade."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('artist-upgrade-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        bound_catalog(patch)
        yield


@pytest.fixture(scope='module')
def predecessor():
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        runner.migrate(conn, registry=registry.MIGRATIONS[:20], clock=lambda: 1.)
        yield conn


def test_predecessor_is_the_actual_pre_artist_v20_layout(predecessor):
    runner.validate_version(predecessor, 20)
    assert not predecessor.execute("SELECT 1 FROM sqlite_schema WHERE name='artists'").fetchone()


def history(conn):
    return tuple(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))


@pytest.fixture(scope='module')
def installed(predecessor, tmp_path_factory):
    path = tmp_path_factory.mktemp('artist-handle-upgrade') / 'installed.db'
    conn = sqlite3.connect(path)
    predecessor.backup(conn)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA journal_mode=WAL')
    with conn:
        # Distinct literal names isolate the two obligations in one database.
        # The redirect pair deliberately share the same name and song title.
        for rid, video, name, provider, key in (
                (1, 'v', 'Artist', None, 'song|artist'),
                (2, 'reconcile', 'Reconciled Artist', None, 'installed:2'),
                (3, 'known', 'Reconciled Artist', 'verified-existing', 'installed:3')):
            conn.execute('INSERT INTO recordings(id,identity_key) VALUES (?,?)', (rid, key))
            conn.execute('INSERT INTO youtube_items(video_id,recording_id,title,artist,artist_browse_id) '
                         "VALUES (?,?,'Song',?,?)", (video, rid, name, provider))
            conn.execute('INSERT INTO catalog_entries(id,recording_id,video_id) VALUES (?,?,?)', (rid, rid, video))
        conn.execute("INSERT INTO rec_feedback(surface,item_key,kind,created_at) "
                     "VALUES ('for_you','artist:Artist','mute',1)")
        conn.execute("INSERT INTO artist_scans VALUES ('Artist',10)")
    state = {'conn': conn, 'path': path, 'numbered': history(conn)}
    try:
        yield state
    finally:
        state['conn'].close()


def test_populated_input_retains_exact_legacy_mute_scan_and_provider_evidence(installed):
    conn = installed['conn']
    assert tuple(conn.execute('SELECT surface,item_key,kind,created_at FROM rec_feedback').fetchone()) == (
        'for_you', 'artist:Artist', 'mute', 1)
    assert tuple(conn.execute('SELECT * FROM artist_scans').fetchone()) == ('Artist', 10)
    assert [tuple(row) for row in conn.execute(
        'SELECT video_id,artist,artist_browse_id FROM youtube_items ORDER BY recording_id')] == [
            ('v', 'Artist', None), ('reconcile', 'Reconciled Artist', None),
            ('known', 'Reconciled Artist', 'verified-existing')]


@pytest.fixture(scope='module')
def credited(installed):
    conn = installed['conn']
    runner.migrate(conn, registry=registry.MIGRATIONS[:26], clock=lambda: 2.)
    ids = dict(conn.execute('SELECT entry_id,artist_id FROM track_artists'))
    installed.update(first=ids[1], old=ids[2], target=ids[3])
    with conn:
        conn.execute('INSERT INTO rec_feedback(surface,item_key,kind,created_at,artist_id) '
                     "VALUES ('for_you',?,'mute',1,?)", (f'artist:@artist:{ids[2]}', ids[2]))
        conn.execute("INSERT INTO artist_scans(artist,scanned_at,artist_id) VALUES ('Reconciled Artist',10,?)", (ids[2],))
    return installed


def test_real_artist_conversion_preserves_name_feedback_and_separate_provisional_handles(credited):
    conn = credited['conn']
    assert len({credited['first'], credited['old'], credited['target']}) == 3
    assert tuple(conn.execute('SELECT provisional FROM artists WHERE id=?', (credited['old'],)).fetchone()) == (1,)
    assert conn.execute("SELECT artist_id FROM artist_provider_ids WHERE provider_id='verified-existing'").fetchone()[0] == credited['target']
    assert conn.execute("SELECT artist_id FROM rec_feedback WHERE item_key='artist:Artist'").fetchone()[0] == credited['first']
    assert history(conn)[:20] == credited['numbered']
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(scope='module')
def historical_reopen(credited):
    credited['conn'].close()
    conn = sqlite3.connect(credited['path'])
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    credited['conn'] = conn
    return credited


def test_fresh_installed_connection_keeps_the_original_numeric_handles(historical_reopen):
    state = historical_reopen
    assert dict(state['conn'].execute('SELECT entry_id,artist_id FROM track_artists')) == {
        1: state['first'], 2: state['old'], 3: state['target']}


def assert_retained(state):
    conn = state['conn']
    assert history(conn)[:20] == state['numbered']
    assert {row[0] for row in conn.execute('SELECT id FROM artists')} >= {
        state['first'], state['old'], state['target']}
    assert {tuple(row) for row in conn.execute('SELECT artist_id,scanned_at FROM artist_scans')} == {
        (state['first'], 10), (state['old'], 10)}
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()
    if 'playback_original' in state:
        assert tuple(conn.execute("SELECT * FROM youtube_items WHERE video_id='original'").fetchone()) == state['playback_original']


@pytest.mark.parametrize('target', range(27, 103))
def test_real_registered_edges_keep_original_artist_ids_feedback_scans_and_history(historical_reopen, target):
    state = historical_reopen
    runner.migrate(state['conn'], registry=registry.MIGRATIONS[:target], clock=lambda: 2.)
    if target == 47:
        # The playback-alternate predecessor shares this genuine historical
        # journey instead of repeating all unrelated artist/catalog upgrades.
        with catalog_reader(state['path'], 47) as store:
            state['playback_account'] = store.accounts.upsert_account('main', 'credential', None, True)
            store.tracks.upsert_track('original', 'Playback Song', 'Playback Artist', '', 100)
            state['playback_original'] = tuple(store.conn.execute(
                "SELECT * FROM youtube_items WHERE video_id='original'").fetchone())
    assert history(state['conn'])[-1][0] == target
    assert_retained(state)
    if target in (27, 29):
        v027_artist_identity_redirects.validate(state['conn'])
        assert not state['conn'].execute('SELECT 1 FROM artist_redirects').fetchone()


def test_account_reference_contains_the_same_artist_and_feedback_owners(historical_reopen):
    definitions = '\n'.join(schema_definition.statements(version=103))
    assert 'CREATE TABLE accounts' in definitions and 'CREATE TABLE artists' in definitions


@pytest.fixture(scope='module')
def renamed(historical_reopen):
    conn = historical_reopen['conn']
    restore, guards = Mock(), Mock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(v103_entity_names, 'restore_programs', restore)
        patch.setattr(regular, 'after_owner_upgrade', guards)
        runner.migrate(conn, registry=registry.MIGRATIONS[:103], clock=lambda: 2.)
    restore.assert_called_once()
    guards.assert_called_once_with(conn)
    historical_reopen['programs'] = restore.call_args
    return historical_reopen


def test_real_account_rename_keeps_numeric_artist_handles(renamed):
    assert renamed['conn'].execute("SELECT 1 FROM sqlite_schema WHERE name='accounts'").fetchone()
    assert_retained(renamed)


@pytest.fixture(scope='module')
def restored(renamed):
    conn, captured = renamed['conn'], renamed['programs']
    with conn:
        conn.execute('BEGIN')
        v103_entity_names.restore_programs(*captured.args, **captured.kwargs)
        regular.after_owner_upgrade(conn)
    return renamed


def test_real_program_restoration_keeps_constraints_and_historical_artist_evidence(restored):
    runner.validate_version(restored['conn'], 103, foreign_keys=False, data=False)
    assert_retained(restored)


@pytest.mark.parametrize('target', range(104, len(registry.MIGRATIONS) + 1))
def test_later_registered_edges_keep_artist_state_before_new_provider_observations(restored, target):
    runner.migrate(restored['conn'], registry=registry.MIGRATIONS[:target], clock=lambda: 2.)
    assert_retained(restored)


@pytest.fixture(scope='module')
def regular_input(restored):
    complete_regular_input(restored['conn'])
    return restored


def test_completed_regular_input_is_admitted_without_rewriting_artist_history(regular_input):
    conn = regular_input['conn']
    before = list(conn.iterdump())
    assert runner.migrate(conn, defer_validation=True) is False
    assert list(conn.iterdump()) == before
    regular.validate(conn)
    assert_retained(regular_input)


@pytest.fixture(scope='module')
def writable(regular_input):
    store = Store(regular_input['path'])
    try:
        store.init_schema()
        yield regular_input, store
    finally:
        store.conn.close()


def test_first_real_structured_playlist_sync_keeps_provisional_id_mute_and_scan(writable):
    state, store = writable
    account = store.accounts.upsert_account('main', 'credential', None, True)
    client = FakeClient(tracks={'P': [{'videoId': 'v', 'title': 'Song', 'duration_seconds': 200,
                                    'artists': [{'name': 'Artist', 'id': 'verified'}]}]})
    refresh_playlist(store, account, client, 'P', 'Mix', 20)
    assert store.artists.track_credits(1)[0]['artist_id'] == state['first']
    assert store.artists.get_artist(state['first'])['provider_ids'] == {'youtube': 'verified'}
    assert 'song|artist' in store.rec_model.suppressed_keys('for_you', 30)
    assert tuple(store.conn.execute('SELECT artist_id,scanned_at FROM artist_scans WHERE artist_id=?',
                                   (state['first'],)).fetchone()) == (state['first'], 10)
    v027_artist_identity_redirects.validate(store.conn)


def test_real_playback_alias_write_keeps_upload_facts_and_remains_account_scoped(writable):
    state, store = writable
    account = state['playback_account']
    assert tuple(store.conn.execute("SELECT * FROM youtube_items WHERE video_id='original'").fetchone()) == state['playback_original']
    assert store.tracks.playback_alternate(account, 'original') is None
    store.tracks.upsert_track('replacement', 'Playback Song', 'Playback Artist', '', 100)
    original = {'video_id': 'original', 'title': 'Playback Song', 'artist': 'Playback Artist', 'duration': 100}
    store.tracks.remember_playback_alternate(account, original,
        {**original, 'video_id': 'replacement'}, evidence={'set_video_id': 'owned'}, now=1)
    store.init_schema()
    assert store.tracks.playback_alternate(account, 'original')['video_id'] == 'replacement'
    other = store.accounts.upsert_account('Other playback', 'other-ref', None, False)
    assert store.tracks.playback_alternate(other, 'original') is None
    assert tuple(store.conn.execute("SELECT * FROM youtube_items WHERE video_id='original'").fetchone()) == state['playback_original']


def test_real_existing_provider_observation_reconciles_the_other_installed_handle(writable):
    state, store = writable
    store.tracks.upsert_track('reconcile', 'Song', 'Reconciled Artist', None, 200,
                             credits=[{'name': 'Reconciled Artist', 'id': 'verified-existing'}])
    assert store.artists.resolve(state['old']) == state['target']
    assert store.artists.track_credits(2)[0]['artist_id'] == state['target']
    assert store.tracks.identity_key_for_video('reconcile') in store.rec_model.suppressed_keys('for_you', 30)


@pytest.fixture(scope='module')
def reopened(writable):
    state, store = writable
    state['before_reopen'] = list(store.conn.iterdump())
    store.conn.close()
    fresh = Store(state['path'])
    try:
        fresh.init_schema()
        yield state, fresh
    finally:
        fresh.conn.close()


def test_fresh_store_reopen_is_immutable_and_retains_both_feedback_contracts(reopened):
    state, store = reopened
    assert list(store.conn.iterdump()) == state['before_reopen']
    v027_artist_identity_redirects.validate(store.conn)
    assert store.artists.track_credits(1)[0]['artist_id'] == state['first']
    assert store.artists.resolve(state['old']) == state['target']
    suppressed = store.rec_model.suppressed_keys('for_you', 30)
    assert 'song|artist' in suppressed
    assert store.tracks.identity_key_for_video('reconcile') in suppressed
    assert store.conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not (state['path'].parent / 'backups').exists()


def test_fresh_reopen_keeps_the_provider_witnessed_playback_alias_and_original_upload(reopened):
    state, store = reopened
    assert store.tracks.playback_alternate(state['playback_account'], 'original')['video_id'] == 'replacement'
    assert tuple(store.conn.execute("SELECT * FROM youtube_items WHERE video_id='original'").fetchone()) == state['playback_original']


def test_old_handle_remains_writable_through_the_real_reopened_discovery_repository(reopened):
    state, store = reopened
    store.discovery.mark_scanned(state['old'], 50)
    assert {tuple(row) for row in store.conn.execute('SELECT artist_id,scanned_at FROM artist_scans')} == {
        (state['first'], 10), (state['target'], 50)}
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
