"""One populated upgrade retains notes, demand and durable collection owners."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import pytest

from tests.catalog_migration_support import catalog_reader
from tests.core.test_album_note_requests_migration import ALBUM
from tests.core.test_catalog import complete_regular_input
from tests.core.test_taste_controls_migration import decision
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import install_numbered_input
from yt_playlist.core import (albums, collection_recovery, experiment_decision_state, playlist_scopes,
    recommendation_outputs as outputs, spotify_job_state, sync_state, taste_control_state)
from yt_playlist.core.migrations import registry, regular, runner, v103_entity_names
from yt_playlist.core.store import Store


NOTES = {ALBUM['browse_id']: {
    'label': 'Label', 'description': None, 'at': 100,
    'links': [{'url': 'https://example.com/release', 'label': 'Release'}]}}
REQUESTS = {ALBUM['browse_id']: ALBUM,
    'unadmitted': {**ALBUM, 'browse_id': 'unadmitted', 'year': 1999, 'requested_until': 1800}}
DEMAND = {
    'choice_window:mixes': {'opportunity_id': 'external-offer', 'window': '07'},
    'album_menu_refresh': {'epoch': 2, 'exclude': ['known-release', 'missing-release', 'known-release']},
    'home_genre_request': {'epoch': 3, 'include_genres': ['jazz', 'other:unregistered style'],
        'avoid_genres': [], 'held_by_vibe': False, 'suspended_by_vibe': True}}
ROAD_STATE = {'picks': ['first', 'first', 'second'], 'banned': ['excluded'],
    'seed': 7, 'building': True, 'revision': 'held'}
JOB = {'status': 'running', 'identity_id': 999, 'identity_label': 'Retired account',
    'started_at': 2, 'report': {'partial': [1, 2]}, 'dismissed': None}
RECIPE = {'media': 'audio', 'model': 'fresh',
    'facets': {'genres': ['house', 'Unknown original', 'house'], 'eras': ['2010']},
    'opaque': {'positions': [2, 2, 1]}}
CANVAS = 'legacy canvas text: {\n  no JSON requirement\n}'
CHECKPOINT = {'saved_at': 5, 'params': {'rec_param:breadth_bias': '.3'},
    'weights': [{'axis': 'genre:An unknown original', 'weight': .4, 'updated_at': None}]}
BASELINE = [{'key': 'unknown|artist', 'rank': None, 'score': .2, 'lane': '',
    'lane_weight': None, 'trace': {'terms': [1, 2]}}]


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL/file owners do not construct an HTTP lifespan."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('note-demand-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        bound_catalog(patch)
        yield


@pytest.fixture(scope='module')
def predecessor(tmp_path_factory):
    path = tmp_path_factory.mktemp('note-demand-upgrade') / 'notes.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        install_numbered_input(conn, 83)
        yield {'conn': conn, 'path': path, 'version': 83,
            'history': tuple(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))}


def test_predecessor_is_the_actual_supported_v83_contract(predecessor):
    runner.validate_version(predecessor['conn'], 83)
    assert predecessor['conn'].execute('PRAGMA foreign_keys').fetchone()[0] == 1


@pytest.fixture(scope='module')
def populated(predecessor):
    with catalog_reader(predecessor['path'], 83) as store, store.conn:
        albums.observe(store.conn, ALBUM['browse_id'], title=ALBUM['title'], artist=ALBUM['artist'])
        albums.observe(store.conn, 'known-release', artist='Artist', title='Album')
        store.conn.executemany('INSERT INTO rec_proposals VALUES (?,?,?)', [
            ('album_notes', json.dumps(NOTES), 99.5),
            ('album_note_requests', json.dumps(REQUESTS), 1234.5),
            *((surface, json.dumps(value), 123.5) for surface, value in DEMAND.items())])
    return predecessor


def test_legacy_source_keeps_every_literal_note_request_and_independent_demand(populated):
    with catalog_reader(populated['path'], 83) as store:
        assert store.rec_proposals.get_proposals('album_notes') == NOTES
        assert store.rec_proposals.get_proposals('album_note_requests') == REQUESTS
        assert {key: store.rec_proposals.get_proposals(key) for key in DEMAND} == DEMAND


@pytest.fixture(scope='module')
def converted(populated):
    runner.migrate(populated['conn'], registry=registry.MIGRATIONS[:84], clock=lambda: 2.)
    populated['version'] = 84
    populated['note'] = dict(populated['conn'].execute('SELECT * FROM album_note_outputs').fetchone())
    populated['bytes'] = Path(populated['note']['filename']).read_bytes()
    return populated


def assert_preserved(state):
    conn = state['conn']
    assert tuple(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))[:83] == state['history']
    assert dict(conn.execute('SELECT * FROM album_note_outputs').fetchone()) == state['note']
    assert Path(state['note']['filename']).read_bytes() == state['bytes']
    with catalog_reader(state['path'], state['version']) as store:
        assert store.rec_proposals.get_proposals('album_notes') == NOTES
        assert store.rec_proposals.get_proposals('album_note_requests') == REQUESTS
        assert {key: store.rec_proposals.get_proposals(key) for key in DEMAND} == DEMAND
        if state['version'] >= 94:
            assert store.road_trip.get_road_trip_draft(state['road']) == ROAD_STATE
            assert list(map(tuple, conn.execute(
                'SELECT selection_state,position,video_id FROM road_trip_draft_entries '
                'ORDER BY selection_state,position'))) == [
                    ('excluded', 0, 'excluded'), ('picked', 0, 'first'),
                    ('picked', 1, 'first'), ('picked', 2, 'second')]
            assert tuple(conn.execute('SELECT account_id,legacy_author_unknown FROM road_trip_drafts').fetchone()) == (None, 1)
            assert tuple(conn.execute('SELECT account_id,legacy_author_unknown,response FROM album_feedback').fetchone()) == (None, 1, 'more')
            assert sync_state.read(conn) == state['sync']
            assert store.settings.get_setting('rec_content_model') == '{"ncat": 0, "cont": []}'
            assert store.settings.get_setting('rec_content_inputs') == 'fingerprint'
            assert not conn.execute("SELECT 1 FROM settings WHERE key IN "
                "('album_feedback','library_sync_state','rec_content_model','rec_content_generation','rec_content_inputs')").fetchone()
        if state['version'] >= 98:
            original, draft, migrated, linked = state['actions']
            assert store.actions.get_draft('SOURCE|OTHER').id == draft
            assert store.actions.get_action(draft).undo_json == 'historical non-JSON draft evidence'
            assert json.loads(store.actions.get_action(original).undo_json) == state['undo']
            assert conn.execute('SELECT undo_of_action_id FROM actions WHERE id=?', (linked,)).fetchone()[0] == migrated
            assert tuple(conn.execute('SELECT phase,outcome,backup_status FROM action_steps').fetchone()) == (
                'uncertain', 'unknown', 'missing_historical')
            assert store.actions.get_pending_deletion(state['account'], 'SOURCE').id == original
            captured = conn.execute('SELECT * FROM actions WHERE id=?', (original,)).fetchone()
            evidence = collection_recovery.read(captured['recovery_filename'], captured['recovery_digest'],
                captured['recovery_bytes'], kind='action', owner=original)
            assert json.loads(evidence['undo']) == state['undo']
        if state['version'] >= 96:
            assert json.loads(store.settings.get_setting(taste_control_state.CHECKPOINT)) == CHECKPOINT
            assert json.loads(store.settings.get_setting(taste_control_state.BASELINE)) == BASELINE
            assert experiment_decision_state.records(conn) == [decision(6)]
            assert tuple(conn.execute('SELECT recording_id,unresolved_key FROM taste_comparison_entries').fetchone()) == (None, 'unknown|artist')
            assert not conn.execute("SELECT 1 FROM settings WHERE key IN "
                "('taste_settings_checkpoint_v1','taste_comparison_baseline_v2','ppr_experiment_decisions')").fetchone()
        if state['version'] >= 97:
            assert json.loads(store.settings.get_setting(spotify_job_state.KEY)) == JOB
            assert store.playlist_recipes.get_recipe('PLAYLIST') == RECIPE
            assert store.playlist_recipes.get_cluster_canvas('PLAYLIST') == CANVAS
            assert tuple(conn.execute('SELECT account_id,unresolved_account_id FROM spotify_import_jobs').fetchone()) == (None, 999)
            assert conn.execute('SELECT count(*) FROM playlist_recipe_facets').fetchone()[0] == 4
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


def test_actual_v84_conversion_keeps_order_unknown_provider_values_and_original_output(converted):
    rows = list(converted['conn'].execute('SELECT * FROM album_note_requests ORDER BY id'))
    assert [(row['album_provider_id'], row['unresolved_reason']) for row in rows] == [
        (ALBUM['browse_id'], None), (None, 'provider_album_not_admitted')]
    assert all(row['requested_at'] is None and row['updated_at'] == 1234.5 for row in rows)
    assert converted['note']['album_provider_id'] == ALBUM['browse_id']
    assert converted['note']['built_at'] == 99.5
    assert not converted['conn'].execute(
        "SELECT 1 FROM rec_proposals WHERE surface IN ('album_notes','album_note_requests')").fetchone()
    assert_preserved(converted)


@pytest.fixture(scope='module')
def recovered(converted, tmp_path_factory):
    with catalog_reader(converted['path'], 84, snapshot_guards=True) as store:
        with store.read_snapshot() as snapshot:
            manifest = outputs.export(snapshot.store.conn, tmp_path_factory.mktemp('note-backup'))
        Path(converted['note']['filename']).unlink()
        outputs.restore(store.conn, manifest)
    return converted


def test_real_snapshot_backup_restores_the_deleted_original_note_and_keeps_pending_demand(recovered):
    assert_preserved(recovered)


def populate_collection_owners(state):
    with catalog_reader(state['path'], 92) as store:
        account = store.accounts.upsert_account('Original', 'original.json', None, True)
        state['account'] = account
        state['road'] = store.road_trip.save_road_trip_recipe(None, 'Trip', 50, [], ['Synth Pop'], 60, 10)
        store.road_trip.save_road_trip_draft(state['road'], ROAD_STATE, 11)
        state['sync'] = {'status': 'partial', 'accounts': {str(account): {'status': 'partial', 'resources': {
            'playlists': {'status': 'partial', 'items': {'LM': {'status': 'failed', 'failures': 2,
                'retry_at': 99, 'last_attempt_at': 13, 'last_success_at': 3}}}}}},
            'resources': {}, 'last_attempt_at': 13}
        values = {'album_feedback': {'release': {'kind': 'more', 'at': 12}},
            'library_sync_state': state['sync'], taste_control_state.CHECKPOINT: CHECKPOINT,
            taste_control_state.BASELINE: BASELINE, 'ppr_experiment_decisions': [decision(6)],
            spotify_job_state.KEY: JOB}
        for key, value in values.items():
            store.settings.set_setting(key, json.dumps(value))
        for key, value in [('rec_content_model', '{"ncat": 0, "cont": []}'),
                ('rec_content_generation', 'generation'), ('rec_content_inputs', 'fingerprint')]:
            store.settings.set_setting(key, value)
        store.playlist_recipes.set_recipe('PLAYLIST', RECIPE, 3)
        store.playlist_recipes.set_cluster_canvas('PLAYLIST', CANVAS, 4)
        # Use the installed physical writer; this reader has TEMP compatibility
        # views for current APIs, so raw predecessor SQL belongs on its owner.
        with state['conn']:
            scope = playlist_scopes.ensure(state['conn'], 'SOURCE', account)
            state['conn'].execute('INSERT INTO playlists(identity_id,ytm_playlist_id,scope_ref,title,track_count) '
                "VALUES (?,'SOURCE',?,'SOURCE',0)", (account, scope))
        state['undo'] = {'deletions': [{'identity': account, 'ytm': 'SOURCE', 'phase': 'uncertain',
            'backup': str(state['path'].parent / 'unavailable-historical-backup.json')}]}
        state['actions'] = (
            store.actions.record_action('gc_generated', '{}', '{}', 'delete_pending', json.dumps(state['undo']), 2),
            store.actions.record_action('merge_draft', '{"keep":"all"}', 'SOURCE|OTHER', 'draft', 'historical non-JSON draft evidence', 3),
            store.actions.record_action('copy_playlist', '{}', '{}', 'undone', '{}', 4))
        state['actions'] += (store.actions.record_action('undo', json.dumps({'undid': state['actions'][2]}), '{}', 'executed', '{}', 5),)
        assert store.road_trip.get_road_trip_draft(state['road']) == ROAD_STATE
        assert store.playlist_recipes.get_recipe('PLAYLIST') == RECIPE
        assert store.playlist_recipes.get_cluster_canvas('PLAYLIST') == CANVAS


@pytest.mark.parametrize('target', range(85, 103))
def test_actual_registered_owners_keep_notes_files_and_all_demand_fields(recovered, target):
    runner.migrate(recovered['conn'], registry=registry.MIGRATIONS[:target], clock=lambda: 2.)
    recovered['version'] = target
    if target == 92:
        populate_collection_owners(recovered)
    assert_preserved(recovered)
    if target >= 87:
        assert list(map(tuple, recovered['conn'].execute(
            'SELECT album_provider_id,unresolved_reason FROM album_menu_refresh_exclusions ORDER BY position'))) == [
                ('known-release', None), (None, 'provider_album_not_admitted'), ('known-release', None)]
        assert list(map(tuple, recovered['conn'].execute(
            'SELECT family_key,genre_key,resolution_reason FROM home_genre_request_items ORDER BY position'))) == [
                ('jazz', None, None), (None, None, 'unknown_genre')]
        assert not recovered['conn'].execute(
            "SELECT 1 FROM rec_proposals WHERE surface IN ('choice_window:mixes','album_menu_refresh','home_genre_request')").fetchone()


@pytest.fixture(scope='module')
def renamed(recovered):
    # The account rename and its dependent programs have distinct owners. Run
    # each once while keeping the actual retained note SQL/files between them.
    runner.migrate(recovered['conn'], registry=registry.MIGRATIONS[:102], clock=lambda: 2.)
    restore, guards = Mock(), Mock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(v103_entity_names, 'restore_programs', restore)
        patch.setattr(regular, 'after_owner_upgrade', guards)
        runner.migrate(recovered['conn'], registry=registry.MIGRATIONS[:103], clock=lambda: 2.)
    restore.assert_called_once()
    guards.assert_called_once_with(recovered['conn'])
    recovered['programs'] = restore.call_args
    recovered['version'] = 103
    return recovered


def test_actual_account_rename_retains_original_notes_and_request_meaning(renamed):
    assert_preserved(renamed)


@pytest.fixture(scope='module')
def restored(renamed):
    conn, captured = renamed['conn'], renamed['programs']
    with conn:
        conn.execute('BEGIN')
        v103_entity_names.restore_programs(*captured.args, **captured.kwargs)
        regular.after_owner_upgrade(conn)
    return renamed


def test_actual_program_restoration_keeps_the_full_v103_contract(restored):
    runner.validate_version(restored['conn'], 103, foreign_keys=False, data=False)
    assert_preserved(restored)


@pytest.mark.parametrize('target', range(104, len(registry.MIGRATIONS) + 1))
def test_actual_later_registered_owners_keep_exact_notes_requests_and_history(restored, target):
    runner.migrate(restored['conn'], registry=registry.MIGRATIONS[:target], clock=lambda: 2.)
    restored['version'] = target
    assert_preserved(restored)


@pytest.fixture(scope='module')
def regular_input(restored):
    runner.migrate(restored['conn'], registry=registry.MIGRATIONS, clock=lambda: 2.)
    restored['version'] = len(registry.MIGRATIONS)
    complete_regular_input(restored['conn'])
    return restored


def test_real_current_admission_keeps_exact_sql_history_and_original_note_bytes(regular_input):
    before = list(regular_input['conn'].iterdump())
    assert runner.migrate(regular_input['conn']) is False
    assert list(regular_input['conn'].iterdump()) == before
    assert_preserved(regular_input)


def test_fresh_store_reopen_preserves_original_notes_and_every_pending_demand(regular_input):
    before = list(regular_input['conn'].iterdump())
    store = Store(regular_input['path'])
    with closing(store.conn):
        store.init_schema()
        assert list(store.conn.iterdump()) == before
        assert store.rec_proposals.get_proposals('album_notes') == NOTES
        assert store.rec_proposals.get_proposals('album_note_requests') == REQUESTS
        assert {key: store.rec_proposals.get_proposals(key) for key in DEMAND} == DEMAND
        assert outputs.collect_orphans(store.conn) == 0
    assert_preserved(regular_input)


def test_reopened_collection_owners_accept_updates_without_rewriting_captured_evidence(regular_input):
    from yt_playlist.web.spotify_import import load_job, save_job

    store = Store(regular_input['path'])
    with closing(store.conn):
        store.init_schema()
        original = regular_input['actions'][0]
        captured = dict(store.conn.execute('SELECT * FROM actions WHERE id=?', (original,)).fetchone())
        with store.conn:
            store.conn.execute("UPDATE action_steps SET phase='remote_complete',outcome='confirmed_absent'")
        assert json.loads(store.actions.get_action(original).undo_json)['deletions'][0]['phase'] == 'remote_complete'
        evidence = collection_recovery.read(captured['recovery_filename'], captured['recovery_digest'],
            captured['recovery_bytes'], kind='action', owner=original)
        assert json.loads(evidence['undo']) == regular_input['undo']
        interrupted = load_job(store)
        interrupted['status'] = 'error'
        save_job(store, interrupted)
        assert load_job(store)['account_id'] == 999
        assert store.rec_model.restore_taste_settings(taste_control_state.CHECKPOINT, ['rec_param:breadth_bias'])
        assert store.settings.get_setting('rec_param:breadth_bias') == '.3'
        assert store.rec_model.weight_records()['genre:An unknown original']['updated_at'] is None
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
