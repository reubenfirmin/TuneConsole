"""A single populated legacy upgrade retains play, weight and mirror custody."""
from contextlib import closing
import json
import sqlite3
from unittest.mock import Mock

import pytest

from tests.core.test_catalog import complete_regular_input
from tests.catalog_migration_support import catalog_reader
from tests.genre_catalog_support import bound_catalog
from tests.group_support import publish, recordings
from tests.mode_fixtures import seed_modes
from yt_playlist.core import embedding_files, schema_definition, taste_axes
from yt_playlist.core.migrations import Migration, MigrationError, registry, regular, runner, v103_entity_names
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL owners do not construct an HTTP lifespan."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('legacy-history-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        bound_catalog(patch)
        yield


def history(conn):
    return tuple(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))


@pytest.fixture(scope='module')
def v7_schema():
    with closing(sqlite3.connect(':memory:')) as conn:
        runner.migrate(conn, registry=registry.MIGRATIONS[:7], clock=lambda: 1.)
        yield conn


def test_legacy_history_predecessor_is_a_real_registered_v7_database(v7_schema):
    runner.validate_version(v7_schema, 7)
    assert v7_schema.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 7


@pytest.fixture(scope='module')
def legacy_input(v7_schema, tmp_path_factory):
    path = tmp_path_factory.mktemp('legacy-history-upgrade') / 'installed.db'
    store = Store(path)
    try:
        v7_schema.backup(store.conn)
        store._bind_repositories()
        with store.conn:
            store.conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (1,'main','cred',1)")
            store.conn.execute("INSERT INTO playlists(identity_id,ytm_playlist_id,title) VALUES (1,'PLGEN','Generated')")
            store.conn.execute("INSERT INTO playlist_group VALUES ('PLGEN','Generated')")
            store.conn.execute("INSERT INTO tracks(video_id,title,artist,identity_key) VALUES ('vid','Song','Artist','song|artist')")
            store.conn.execute("INSERT INTO play_events(identity_id,identity_key,video_id,played_at,playlist_ytm_id) "
                               "VALUES (1,'song|artist','vid',120,'RDAMPLPLGEN')")
            store.conn.execute("INSERT INTO settings VALUES ('rec_param:breadth_bias','-0.5')")
        store.rec_model.set_weight('lane:explore', 1.3, now=1000)
        store.rec_model.set_weight('lane:deep_cut', 1.7, now=900)
        store.rec_model.set_weight('genre:jazz', 0, lo=0, now=800)
        weights = {axis: row for axis, row in store.rec_model.weight_records().items() if axis != 'lane:explore'}
        params = ('rec_param:breadth_bias',)
        store.rec_model.save_taste_settings('taste_settings_checkpoint_v1', params, 1000)
        with store.conn:
            store.conn.execute('ALTER TABLE play_events DROP COLUMN is_generated')
            # The legacy discovery owner predates the separate exposure archive.
            store.conn.execute('DROP TABLE discovered_albums')
            store.conn.execute('CREATE TABLE discovered_albums ('
                'browse_id TEXT PRIMARY KEY, artist TEXT, title TEXT, year TEXT, thumbnail TEXT,'
                'found_at REAL, last_shown REAL, genre TEXT, first_shown REAL,'
                'offered_count INTEGER NOT NULL DEFAULT 0)')
            store.conn.execute('INSERT INTO discovered_albums VALUES (?,?,?,?,?,?,?,?,?,?)',
                ('album', 'Rita Payés', 'Rueda Eterna', '2026', None, 100, 300, None, 200, 18))
            store.conn.execute('DROP TABLE schema_migrations')
    finally:
        store.conn.close()
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        state = dict(conn=conn, path=path, weights=weights, params=params)
        try:
            yield state
        finally:
            if 'v17_source' in state:
                state['v17_source'].close()


def test_unversioned_source_keeps_unknown_play_provenance_and_the_original_weight_checkpoint(legacy_input):
    conn = legacy_input['conn']
    assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='schema_migrations'").fetchone()
    assert 'is_generated' not in {row[1] for row in conn.execute('PRAGMA table_info(play_events)')}
    assert tuple(conn.execute('SELECT identity_key,video_id,played_at,playlist_ytm_id FROM play_events').fetchone()) == (
        'song|artist', 'vid', 120, 'RDAMPLPLGEN')
    assert conn.execute("SELECT weight FROM rec_weights WHERE axis='lane:explore'").fetchone()[0] == 1.3
    assert 'legacy_offered_count' not in {row[1] for row in conn.execute('PRAGMA table_info(discovered_albums)')}
    assert tuple(conn.execute('SELECT artist,title,offered_count,first_shown,last_shown '
                              "FROM discovered_albums WHERE browse_id='album'").fetchone()) == (
        'Rita Payés', 'Rueda Eterna', 18, 200, 300)


def assert_preserved(state, version):
    conn = state['conn']
    if version >= 1:
        assert conn.execute('SELECT is_generated FROM play_events WHERE played_at=120').fetchone()[0] is None
        relation = ("discovered_albums WHERE browse_id='album'" if version < 23 else
            "album_discovery_memberships WHERE album_id=(SELECT album_id FROM album_provider_ids "
            "WHERE provider='youtube' AND provider_id='album')")
        assert tuple(conn.execute('SELECT legacy_offered_count,legacy_first_shown,legacy_last_shown,'
            'offered_count,first_shown FROM ' + relation).fetchone()) == (
                18, 200, 300, 0, None)
    if version >= 5:
        assert taste_axes.lookup(conn, 'rec_weights', 'lane:explore', 'weight') is None
    if version >= 7:
        assert history(conn)[:7] == state['v7_history']
        owner = 'identities' if version < 103 else 'accounts'
        assert tuple(conn.execute('SELECT id,label,credential_ref,is_master FROM ' + owner).fetchone()) == (
            1, 'main', 'cred', 1)
    if 18 <= version < 101:
        assert list(map(tuple, conn.execute('SELECT source_token,source_type,source_id,last_found_at '
            'FROM discovery_sources WHERE recording_id=200 ORDER BY source_token'))) == [
                ('album-a', 'album', 'album-a', None), ('radio:seed', 'radio', 'seed', 12)]
    elif version >= 101:
        assert list(map(tuple, conn.execute('SELECT o.legacy_source_key,o.resolution_status,s.last_found_at '
            'FROM discovery_sources s JOIN discovery_origins o ON o.id=s.origin_id '
            'WHERE s.recording_id=200 ORDER BY o.legacy_source_key'))) == [
                ('album-a', 'unresolved', None), ('radio:seed', 'unresolved', 12)]
    if version >= 17:
        assert history(conn)[:17] == state['v17_history']
    if version >= 43:
        assert not conn.execute("SELECT 1 FROM settings WHERE key='rec_param:palette_absence_penalty'").fetchone()
        assert conn.execute("SELECT value FROM settings WHERE key='rec_param:breadth_bias'").fetchone()[0] == '-0.5'
        assert conn.execute("SELECT value FROM settings WHERE key='lastfm_api_key'").fetchone()[0] == 'keep'
    if version >= 11:
        assert not conn.execute('SELECT 1 FROM takeout_imports').fetchone()
    if version >= 31:
        assert conn.execute("SELECT value FROM settings WHERE key='spotify_legacy_discovery_unattributed'").fetchone()[0] == '1'
    if version >= 33:
        account = 'identity_id' if version < 103 else 'account_id'
        assert tuple(conn.execute('SELECT ' + account + ",raw_identity_key,source,precision FROM listening_observations "
            "WHERE raw_identity_key='legacy'").fetchone()) == (1, 'legacy', 'unknown', 'legacy_unknown')
    if version >= 53:
        if version < 70:
            assert conn.execute("SELECT configuration FROM rec_serving_policies WHERE model_version='legacy-policy'").fetchone()[0] == state['policy_raw']
        else:
            from yt_playlist.core.policy_documents import configuration
            assert configuration(conn, 'legacy-policy') == json.loads(state['policy_raw'])
    if version >= 46:
        assert tuple(conn.execute('SELECT * FROM playlists WHERE id=7').fetchone()) == state['mirror']
        assert conn.execute("SELECT value FROM settings WHERE key='personal-setting'").fetchone()[0] == 'keep'
        if version < 68:
            assert tuple(conn.execute("SELECT * FROM rec_vectors WHERE identity_key='song|artist'").fetchone()) == (
                'song|artist', b'\x01\x02\x03\x04')
        else:
            rows = conn.execute('SELECT e.recording_id,e.vector,e.dimensions,e.publication,e.position FROM '
                + embedding_files.relation(conn, 'recording') + ' e '
                'JOIN recordings r ON r.id=e.recording_id '
                "WHERE r.identity_key='song|artist' AND e.space='collaborative'").fetchall()
            assert embedding_files.load_many(conn, 'recording', rows) == [
                (state['mirror_recording'], b'\x01\x02\x03\x04')]
        assert history(conn)[:46] == state['v46_history']
    if version >= 51:
        assert list(map(tuple, conn.execute('SELECT * FROM taste_modes'))) == state['modes']
        assert conn.execute('SELECT count(*) FROM rec_mode_impressions').fetchone()[0] == 2
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('target', range(1, 103))
def test_real_registered_edge_preserves_unknown_history_weights_and_installed_mirror(legacy_input, target):
    conn = legacy_input['conn']
    runner.migrate(conn, registry=registry.MIGRATIONS[:target], clock=lambda: 2.)
    assert history(conn)[-1][0] == target
    if target == 7:
        legacy_input['v7_history'] = history(conn)
    if target == 10:
        with conn:
            conn.execute('INSERT INTO history_snapshots(id,identity_id,taken_at) VALUES (1,1,100)')
            conn.execute("INSERT INTO history_items(snapshot_id,identity_key) VALUES (1,'legacy')")
    if target == 17:
        with conn:
            conn.execute("INSERT INTO recordings(id,identity_key) VALUES (200,'legacy')")
            conn.execute("INSERT INTO discovery_sources VALUES (200,'radio:seed',NULL,12)")
            conn.execute("INSERT INTO discovery_sources VALUES (200,'album-a',NULL,NULL)")
        source = sqlite3.connect(':memory:')
        conn.backup(source)
        source.execute('PRAGMA query_only=ON')
        legacy_input['v17_source'] = source
        legacy_input['v17_history'] = history(conn)
    if target == 42:
        with conn:
            conn.executemany('INSERT INTO settings VALUES (?,?)', [
                ('rec_param:palette_absence_penalty', '1.5'), ('lastfm_api_key', 'keep')])
    if target == 30:
        with conn:
            conn.execute("INSERT INTO listening_calendars VALUES (1,'America/Costa_Rica',1) "
                "ON CONFLICT(identity_id) DO UPDATE SET timezone=excluded.timezone,confirmed=1")
            conn.execute('INSERT INTO listening_observations(identity_id,raw_identity_key,identity_key,source_day,day,'
                'source,timezone,precision,first_observed_at,last_observed_at) '
                "VALUES (1,'old-song','old-song','2024-01-01',NULL,'spotify','UTC','calendar_day',100,100)")
    if target == 52:
        legacy_input['policy_raw'] = '{"ranker":"legacy","evidence":"' + 'x' * 1000000 + '"}'
        with conn:
            conn.execute('INSERT INTO rec_serving_policies VALUES (?,?,1)', ('legacy-policy', legacy_input['policy_raw']))
            conn.execute("INSERT INTO settings VALUES ('active-session','{\"pool_policy\":\"legacy-policy\"}')")
        legacy_input['policy_pages'] = conn.execute('PRAGMA page_count').fetchone()[0]
    if target == 53:
        assert conn.execute('PRAGMA page_count').fetchone()[0] - legacy_input['policy_pages'] < 32
        assert not conn.execute('SELECT 1 FROM rec_policy_bases').fetchone()
    if target == 55:
        legacy_input['audio_run'] = '77f3bd2c-6dba-44fb-b312-fb0104bdca0a'
        with conn:
            conn.execute("INSERT INTO settings VALUES ('audio_test_sentinel','keep')")
            conn.execute('INSERT INTO audio_vibe_feedback VALUES (?,?,?,?,?)', (
                legacy_input['audio_run'], '["historical-excerpt"]', '{"version":"old"}', 'focus', 5))
            conn.execute('INSERT INTO audio_vibe_labels VALUES (?,?,?,?,?,?)', (
                legacy_input['audio_run'], 'audio-vibe-targets-1', '["four-target-excerpt"]',
                '{"version":"previous"}', '["gentle","move"]', 6))
    if target == 46:
        # This independently populated mirror enters at its genuine v46 boundary.
        with conn:
            conn.execute("INSERT INTO playlist_scopes VALUES ('PL','PL',NULL,'shared')")
            conn.execute("INSERT INTO playlist_membership_refs(id,scope_ref,identity_id) VALUES (7,'PL',1)")
            conn.execute("INSERT INTO playlists(id,identity_id,ytm_playlist_id,title,track_count,content_hash,"
                         "first_seen,last_seen,last_changed,scope_ref) VALUES (7,1,'PL','Keep me',0,'hash',10,20,10,'PL')")
            conn.execute("INSERT OR IGNORE INTO recordings(identity_key) VALUES ('song|artist')")
            conn.execute("INSERT INTO rec_vectors VALUES ('song|artist',x'01020304')")
            conn.execute("INSERT INTO settings VALUES ('personal-setting','keep')")
        legacy_input['mirror'] = tuple(conn.execute('SELECT * FROM playlists WHERE id=7').fetchone())
        legacy_input['mirror_recording'] = conn.execute("SELECT id FROM recordings WHERE identity_key='song|artist'").fetchone()[0]
        legacy_input['v46_history'] = history(conn)
        runner.validate_version(conn, 46)
    if target == 51:
        # Retain the actual pre-group writer and its feedback at its v51 boundary.
        with catalog_reader(legacy_input['path'], 51) as store:
            seed_modes(store, 1, 2)
            store.modes.log_impressions(1, [(0, 1), (1, 2)], 10)
        legacy_input['modes'] = list(map(tuple, conn.execute('SELECT * FROM taste_modes')))
        runner.validate_version(conn, 51)
    assert_preserved(legacy_input, target)


def test_interrupted_real_v18_conversion_rolls_back_original_sources_rows_and_history(legacy_input):
    with closing(sqlite3.connect(':memory:')) as conn:
        legacy_input['v17_source'].backup(conn)
        before = list(conn.iterdump())

        def interrupted(connection, context):
            registry.MIGRATIONS[17].apply(connection, context)
            raise RuntimeError('interrupted discovery conversion')

        with pytest.raises(MigrationError, match='interrupted discovery conversion'):
            runner.migrate(conn, registry=(*registry.MIGRATIONS[:17],
                Migration(18, 'typed_discovery_sources', interrupted)))
        assert list(conn.iterdump()) == before
        assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 17


def test_legacy_history_account_reference_keeps_the_registered_rename_contract():
    assert 'CREATE TABLE accounts' in '\n'.join(schema_definition.statements(version=103))


@pytest.fixture(scope='module')
def renamed(legacy_input):
    conn = legacy_input['conn']
    runner.migrate(conn, registry=registry.MIGRATIONS[:102], clock=lambda: 2.)
    restore, guards = Mock(), Mock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(v103_entity_names, 'restore_programs', restore)
        patch.setattr(regular, 'after_owner_upgrade', guards)
        runner.migrate(conn, registry=registry.MIGRATIONS[:103], clock=lambda: 2.)
    restore.assert_called_once()
    guards.assert_called_once_with(conn)
    legacy_input['programs'] = restore.call_args
    return legacy_input


def test_real_account_cutover_keeps_play_provenance_weights_and_mirror(renamed):
    assert renamed['conn'].execute("SELECT 1 FROM accounts WHERE id=1").fetchone()
    assert_preserved(renamed, 103)


@pytest.fixture(scope='module')
def restored(renamed):
    conn, captured = renamed['conn'], renamed['programs']
    with conn:
        conn.execute('BEGIN')
        v103_entity_names.restore_programs(*captured.args, **captured.kwargs)
        regular.after_owner_upgrade(conn)
    return renamed


def test_actual_program_restoration_keeps_historical_state_and_guards(restored):
    runner.validate_version(restored['conn'], 103, foreign_keys=False, data=False)
    assert_preserved(restored, 103)


@pytest.mark.parametrize('target', range(104, len(registry.MIGRATIONS) + 1))
def test_later_registered_edges_keep_the_same_play_weight_and_mirror_custody(restored, target):
    runner.migrate(restored['conn'], registry=registry.MIGRATIONS[:target], clock=lambda: 2.)
    assert_preserved(restored, target)


@pytest.fixture(scope='module')
def regular_input(restored):
    complete_regular_input(restored['conn'])
    return restored


def test_production_admission_retains_the_completed_histories_without_rewriting_rows(regular_input):
    conn = regular_input['conn']
    before = list(conn.iterdump())
    assert runner.migrate(conn, defer_validation=True) is False
    assert list(conn.iterdump()) == before
    regular.validate(conn)
    assert_preserved(regular_input, len(registry.MIGRATIONS))


@pytest.fixture(scope='module')
def current_store(regular_input):
    store = Store(regular_input['path'])
    try:
        store.init_schema()
        regular_input['discovery_rows'] = list(map(tuple, store.conn.execute(
            'SELECT s.*,o.legacy_source_key,o.resolution_status FROM discovery_sources s '
            'JOIN discovery_origins o ON o.id=s.origin_id ORDER BY o.legacy_source_key')))
        yield store
    finally:
        store.conn.close()


def test_current_weight_restore_cannot_resurrect_retired_explore_weights(current_store, regular_input):
    assert current_store.rec_model.weight_records() == regular_input['weights']
    assert current_store.rec_model.restore_taste_settings('taste_settings_checkpoint_v1', regular_input['params'])
    assert current_store.rec_model.weight_records() == regular_input['weights']


def test_current_history_keeps_unknown_coverage_authority_and_daily_spotify_influence(current_store):
    assert tuple(current_store.conn.execute("SELECT identity_key,source FROM listening_observation_subjects "
        "WHERE raw_identity_key='legacy'").fetchone()) == ('legacy', 'unknown')
    assert not current_store.conn.execute('SELECT 1 FROM takeout_imports').fetchone()
    assert current_store.history.spotify_history_stats()['days'] == 0
    assert current_store.history.spotify_history_stats()['unresolved_days'] == 1


def test_current_policy_reader_retains_the_exact_large_installed_configuration(current_store, regular_input):
    assert current_store.rec_ledger.policy_configuration('legacy-policy') == json.loads(regular_input['policy_raw'])
    assert current_store.settings.get_setting('active-session') == '{"pool_policy":"legacy-policy"}'


def test_upgraded_audio_keeps_prior_taxonomies_separate_from_new_feedback(current_store, regular_input):
    from tests.test_audio_vibes import sample
    from yt_playlist.rec import audio_vibe_feedback, audio_vibe_prediction

    run_id = regular_input['audio_run']
    assert current_store.settings.get_setting('audio_test_sentinel') == 'keep'
    value = sample(run_id=run_id)
    current_store.audio_vibes.save_sample(value, 10)
    assert current_store.audio_vibes.feedback(run_id) is None
    assert len(current_store.audio_vibes.earlier_feedback(run_id)) == 2
    audio_vibe_feedback.save(current_store, run_id, [value['id']], ['relax', 'cruise'],
        audio_vibe_prediction.TAXONOMY_VERSION, 11)


def test_fresh_audio_reopen_keeps_samples_original_feedback_and_current_taxonomy(current_store, regular_input):
    reopened = Store(regular_input['path'])
    try:
        reopened.init_schema(defer_metadata=True)
        run_id = regular_input['audio_run']
        assert reopened.conn.execute('SELECT count(*) FROM sample_attributes').fetchone()[0] == 1
        assert reopened.audio_vibes.legacy_feedback(run_id) == {
            'vibe': 'focus', 'sample_ids': ['historical-excerpt'], 'prediction': {'version': 'old'}, 'updated_at': 5}
        assert reopened.audio_vibes.feedback(run_id)['vibes'] == ['relax', 'cruise']
        assert reopened.audio_vibes.earlier_feedback(run_id)[0] == {
            'taxonomy_version': 'audio-vibe-targets-1', 'vibes': ['gentle', 'move'],
            'sample_ids': ['four-target-excerpt'], 'prediction': {'version': 'previous'}, 'updated_at': 6}
    finally:
        reopened.conn.close()


def test_real_current_sql_write_rejects_an_old_playlist_snapshot_and_keeps_the_newer_title(current_store):
    before = current_store.playlists.playlist_revision(1, 'PL')
    with current_store.conn:
        current_store.conn.execute("UPDATE playlists SET title='Newer' WHERE id=7")
    assert current_store.playlists.publish_playlist_snapshot(1, 'PL', 'Old response', [], 100,
                                                            expected_revision=before) is None
    assert current_store.playlists.get_playlist(7).title == 'Newer'
    assert current_store.rec_model.get_rec_vectors() == [('song|artist', b'\x01\x02\x03\x04')]
    assert current_store.settings.get_setting('personal-setting') == 'keep'


def test_real_generated_play_write_retains_its_provenance_after_group_changes(current_store):
    assert current_store.conn.execute('SELECT is_generated FROM play_events WHERE played_at=120').fetchone()[0] is None
    current_store.tracks.upsert_track('new', 'New', 'Artist', None, 200)
    key = current_store.tracks.identity_key_for_video('new')
    current_store.history.record_play_event(1, key, 'new', 5000, playlist_ytm_id='PLGEN')
    current_store.playlists.set_playlist_group('PLGEN', '')
    assert [r[0] for r in current_store.conn.execute('SELECT is_generated FROM play_events ORDER BY id')] == [None, 1]


def test_legacy_server_counts_remain_separate_from_the_first_real_visible_impression(current_store):
    album = current_store.discovery.discovery_album_view(400., 30)[0]
    assert album['browse_id'] == 'album'
    assert album['legacy_offered_count'] == 18
    assert album['offered_count'] == 0 and album['first_shown'] is None and album['gc_at'] is None
    current_store.discovery.record_discovery_impressions('album', ['album'], 'visit', 500.)


@pytest.mark.parametrize('now', [1000, 2000, 3000])
def test_real_group_publications_keep_both_legacy_versions_and_unknown_historical_membership(
        current_store, regular_input, now):
    tracks = recordings([('Pop', 12, .2)])
    before = current_store.taste_groups.head(members=True) if now != 1000 else None
    head = publish(current_store, tracks, now, before)
    legacy = [group for group in head['groups'] if group.get('legacy')]
    if now == 1000:
        assert len(legacy) == 2
        assert all(not group['membership_known'] and not group['members'] for group in legacy)
        regular_input['legacy_versions'] = current_store.taste_groups.legacy_versions()
    assert len(current_store.taste_groups.legacy_versions()) == 2
    assert current_store.taste_groups.legacy_versions() == regular_input['legacy_versions']
    assert list(map(tuple, current_store.conn.execute('SELECT * FROM taste_modes'))) == regular_input['modes']
    assert current_store.conn.execute('SELECT count(*) FROM rec_mode_impressions').fetchone()[0] == 2
    assert not current_store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_fresh_store_reopen_keeps_unknown_and_generated_plays_weights_and_playlist_revisions(current_store, regular_input):
    revision = current_store.playlists.playlist_revision(1, 'PL')
    group_head = current_store.taste_groups.head(members=True)
    before = list(current_store.conn.iterdump())
    current_store.conn.close()
    reopened = Store(regular_input['path'])
    try:
        reopened.init_schema()
        assert list(reopened.conn.iterdump()) == before
        assert [r[0] for r in reopened.conn.execute('SELECT is_generated FROM play_events ORDER BY id')] == [None, 1]
        assert reopened.rec_model.weight_records() == regular_input['weights']
        assert reopened.playlists.playlist_revision(1, 'PL') == revision
        assert reopened.playlists.get_playlist(7).title == 'Newer'
        album = reopened.discovery.discovery_album_view(600., 30)[0]
        assert album['legacy_offered_count'] == 18 and album['offered_count'] == 1
        assert album['first_shown'] == 500.
        assert reopened.taste_groups.head(members=True) == group_head
        assert reopened.taste_groups.legacy_versions() == regular_input['legacy_versions']
        assert list(map(tuple, reopened.conn.execute('SELECT * FROM taste_modes'))) == regular_input['modes']
        assert reopened.conn.execute('SELECT count(*) FROM rec_mode_impressions').fetchone()[0] == 2
        assert not reopened.conn.execute('SELECT 1 FROM takeout_imports').fetchone()
        assert reopened.conn.execute("SELECT count(*) FROM listening_observations WHERE raw_identity_key='legacy'").fetchone()[0] == 1
        assert reopened.history.spotify_history_stats()['unresolved_days'] == 1
        assert reopened.rec_ledger.policy_configuration('legacy-policy') == json.loads(regular_input['policy_raw'])
        assert reopened.conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchone()
        assert [(account.id, account.label) for account in reopened.accounts.get_accounts()] == [(1, 'main')]
        assert reopened.accounts.get_master_account().id == 1
        assert list(map(tuple, reopened.conn.execute(
            'SELECT s.*,o.legacy_source_key,o.resolution_status FROM discovery_sources s '
            'JOIN discovery_origins o ON o.id=s.origin_id ORDER BY o.legacy_source_key'))) == regular_input['discovery_rows']
        assert list(map(tuple, reopened.conn.execute(
            'SELECT o.legacy_source_key,o.resolution_status,s.last_found_at FROM discovery_sources s '
            'JOIN discovery_origins o ON o.id=s.origin_id WHERE s.recording_id=200 ORDER BY o.legacy_source_key'))) == [
                ('album-a', 'unresolved', None), ('radio:seed', 'unresolved', 12)]
        assert not (regular_input['path'].parent / 'backups').exists()
    finally:
        reopened.conn.close()
