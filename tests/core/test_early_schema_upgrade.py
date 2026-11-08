"""Literal early SQL crosses each real owner once, with independent contracts."""
from contextlib import closing
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import pytest

from tests.core.test_current_schema import objects
from tests.genre_catalog_support import bound_catalog
from tests.regular_schema_support import regular_expectation, assert_regular_expectation
from yt_playlist.core import schema_definition
from yt_playlist.core.migrations import current, registry, regular, runner, v103_entity_names
from yt_playlist.core.store import Store


FIXTURES = Path(__file__).parents[1] / 'fixtures/migrations'


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """SQL and admission owners do not construct an HTTP lifespan."""


@pytest.fixture(scope='module', autouse=True)
def isolated_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('early-upgrade-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        bound_catalog(patch, entries=(('house', 'House'), ('rock', 'Rock'), ('jazz', 'Jazz')))
        yield


@pytest.fixture(scope='module')
def early(tmp_path_factory):
    path = tmp_path_factory.mktemp('early-schema') / 'legacy.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript((FIXTURES / 'early_core.sql').read_text())
        conn.executescript('''
            CREATE TABLE play_events (id INTEGER PRIMARY KEY,identity_id INTEGER NOT NULL REFERENCES identities(id),
                identity_key TEXT NOT NULL,video_id TEXT,played_at REAL NOT NULL,playlist_ytm_id TEXT,like_status TEXT);
            INSERT INTO play_events VALUES (9,3,'original title|artist','original-video',100,NULL,NULL);
        ''')
        store = Store(path)
        assert not hasattr(store, 'tracks')
        try:
            yield dict(conn=conn, path=path, store=store, version=0)
        finally:
            store.conn.close()


def test_literal_early_input_keeps_original_ids_and_the_unconstrained_play_ledger(early):
    conn = early['conn']
    assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='schema_migrations'").fetchone()
    assert tuple(conn.execute('SELECT id,title,artist FROM tracks').fetchone()) == (17, 'Original title', 'Artist')
    assert tuple(conn.execute('SELECT * FROM playlist_tracks').fetchone()) == (8, 17, 0)
    assert 'CHECK' not in conn.execute("SELECT sql FROM sqlite_schema WHERE name='play_events'").fetchone()[0]


def seed_owner_inputs(state, target):
    conn = state['conn']
    if target == 1:
        with conn:
            conn.executemany('INSERT INTO settings VALUES (?,?)', [
                ('personal-setting', 'retain'), ('rec_param:onboard_coverage_min', '0.9'),
                ('rec_param:onboard_library_min', '200'), ('rec_param:onboard_feedback_min', '15'),
                ('onboard_dismissed', '1'), ('last_sync_at', '1000')])
            conn.execute('INSERT INTO history_snapshots VALUES (1,3,100)')
            conn.execute("INSERT INTO history_items(snapshot_id,identity_key) VALUES (1,'original title|artist')")
    if target in (8, 9):
        installed_sql = (FIXTURES / 'play_events_subject_v8.sql').read_text().strip()
        assert conn.execute("SELECT sql FROM sqlite_schema WHERE name='play_events_subject'").fetchone()[0] == installed_sql
        if target == 8:
            with conn:
                conn.execute("INSERT INTO recordings(id,identity_key) VALUES (1,'song|artist')")
                conn.execute("INSERT INTO youtube_items(video_id,recording_id) VALUES ('known',1)")
                for event_id, video in enumerate(('', '   ', None, 'known', 'unknown'), 30):
                    conn.execute('INSERT INTO play_events(id,identity_id,identity_key,video_id,played_at,source,is_generated) '
                        "VALUES (?,3,'song|artist',?,?,'live',1)", (event_id, video, event_id))
                conn.execute("INSERT INTO rec_weights VALUES ('genre:house',1.4,1)")
                conn.execute("INSERT INTO rec_vectors VALUES ('song|artist',x'01020304')")
            state['raw_events'] = list(map(tuple, conn.execute(
                'SELECT id,raw_identity_key,video_id,played_at,source,is_generated FROM play_events ORDER BY id')))
        runner.validate_version(conn, target)
    if target == 31:
        with conn:
            conn.executemany('INSERT INTO play_events(identity_id,identity_key,video_id,played_at,is_generated,source) '
                "VALUES (3,'original title|artist','original-video',?,?,'live')", [(1, None), (2, 0), (3, 1)])
        state['events'] = list(map(tuple, conn.execute(
            'SELECT id,video_id,played_at,is_generated FROM play_events ORDER BY id')))
        state['calendar'] = list(map(tuple, conn.execute('SELECT event_id,day FROM play_event_calendar ORDER BY event_id')))
        state['sources'] = list(map(tuple, conn.execute('SELECT event_id,source FROM play_event_sources ORDER BY event_id')))


def assert_originals(state, target):
    conn = state['conn']
    assert tuple(conn.execute('SELECT id,title,artist FROM tracks WHERE id=17').fetchone()) == (17, 'Original title', 'Artist')
    assert tuple(conn.execute('SELECT * FROM playlist_tracks').fetchone()) == (8, 17, 0)
    assert conn.execute("SELECT value FROM settings WHERE key='user-setting'").fetchone()[0] == 'keep me'
    assert conn.execute("SELECT value FROM settings WHERE key='personal-setting'").fetchone()[0] == 'retain'
    if target < 33:
        assert tuple(conn.execute('SELECT snapshot_id,identity_key FROM history_items').fetchone()) == (1, 'original title|artist')
    else:
        assert conn.execute('SELECT count(*) FROM listening_observations').fetchone()[0] == 1
    if target >= 31:
        assert list(map(tuple, conn.execute('SELECT id,video_id,played_at,is_generated FROM play_events ORDER BY id'))) == state['events']
        assert list(map(tuple, conn.execute('SELECT event_id,day FROM play_event_calendar ORDER BY event_id'))) == state['calendar']
        assert list(map(tuple, conn.execute('SELECT event_id,source FROM play_event_sources ORDER BY event_id'))) == state['sources']
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()
    assert not (state['path'].parent / 'backups').exists()


def assert_declared_contract(conn, target):
    statements = current.statements(version=target)
    if target < 32:
        # v001 adds the missing column to this literal predecessor. Its CHECK
        # is deliberately absent until the real v032 table replacement.
        statements = [sql.replace('CHECK(is_generated IN (0,1))', '') if sql.startswith('CREATE TABLE play_events ')
            else sql for sql in statements]
    assert objects(conn) == schema_definition._schema_contract(tuple(statements))


@pytest.mark.parametrize('target', range(1, 103))
def test_actual_registered_edge_keeps_original_rows_history_and_declared_contract(early, target):
    conn = early['conn']
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) if target > 1 else []
    runner.migrate(conn, registry=registry.MIGRATIONS[:target], clock=lambda: 2.)
    early['version'] = target
    assert list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))[:len(history)] == history
    seed_owner_inputs(early, target)
    assert_originals(early, target)
    assert_declared_contract(conn, target)


@pytest.fixture(scope='module')
def renamed(early):
    restore, guards = Mock(), Mock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(v103_entity_names, 'restore_programs', restore)
        patch.setattr(regular, 'after_owner_upgrade', guards)
        runner.migrate(early['conn'], registry=registry.MIGRATIONS[:103], clock=lambda: 2.)
    restore.assert_called_once()
    guards.assert_called_once_with(early['conn'])
    early['programs'] = restore.call_args
    early['version'] = 103
    return early


def test_actual_account_rename_keeps_original_early_rows_and_play_evidence(renamed):
    assert_originals(renamed, 103)
    assert tuple(renamed['conn'].execute('SELECT id,label,credential_ref FROM accounts').fetchone()) == (3, 'main', 'credential')


@pytest.fixture(scope='module')
def restored(renamed):
    captured = renamed['programs']
    with renamed['conn']:
        renamed['conn'].execute('BEGIN')
        v103_entity_names.restore_programs(*captured.args, **captured.kwargs)
        regular.after_owner_upgrade(renamed['conn'])
    return renamed


def test_real_rename_programs_restore_the_declared_contract(restored):
    assert_declared_contract(restored['conn'], 103)
    assert_originals(restored, 103)


@pytest.mark.parametrize('target', range(104, len(registry.MIGRATIONS)+1))
def test_actual_later_edge_keeps_originals_and_exact_declared_contract(restored, target):
    runner.migrate(restored['conn'], registry=registry.MIGRATIONS[:target], clock=lambda: 2.)
    restored['version'] = target
    assert_originals(restored, target)
    assert_declared_contract(restored['conn'], target)


@pytest.fixture(scope='module')
def regular_plan(restored):
    return regular_expectation(restored['conn'], clock=1234)


def test_regular_admission_oracle_captures_every_original_owner_without_writing(restored, regular_plan):
    assert len(regular_plan['rows']['schema_migrations']) == len(registry.MIGRATIONS)
    assert regular_plan['rows']['playlist_items'] == [('PLAYLIST', 'original-video', 0)]


@pytest.mark.parametrize('target', range(1, len(regular.CHANGES)+1))
def test_real_regular_change_preserves_populated_numbered_history(restored, regular_plan, target):
    conn = restored['conn']
    before = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    regular.migrate(conn, changes=regular.CHANGES[:target], clock=lambda: 1234.)
    assert list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == before
    assert list(map(tuple, conn.execute('SELECT version,name FROM schema_changes ORDER BY version'))) == [
        (c.version, c.name) for c in regular.CHANGES[:target]]
    assert_originals(restored, len(registry.MIGRATIONS))


def test_completed_regular_admission_matches_independent_every_row_and_program_oracle(restored, regular_plan):
    assert_regular_expectation(restored['conn'], regular_plan)
    current.validate(restored['conn'])


def test_early_upgraded_schema_matches_fresh_current_objects(restored):
    with closing(sqlite3.connect(':memory:')) as fresh:
        runner.migrate(fresh)
        assert objects(restored['conn'], canonical_versions=True) == objects(fresh, canonical_versions=True)


def test_early_store_binds_real_repositories_and_keeps_unknown_and_frozen_event_evidence(restored):
    store = restored['store']
    store.init_schema()
    assert store.settings.get_setting('user-setting') == 'keep me'
    assert float(store.settings.get_setting('last_sync_at')) == 1000
    settings = dict(store.conn.execute('SELECT key,value FROM settings'))
    assert 'rec_param:onboard_coverage_min' not in settings
    assert 'rec_param:onboard_library_min' not in settings
    assert settings['rec_param:onboard_feedback_min'] == '15'
    assert settings['onboard_dismissed'] == '1'
    assert tuple(store.conn.execute('SELECT id,orig_title,orig_artist FROM tracks WHERE id=17').fetchone()) == (17, 'Original title', 'Artist')
    assert tuple(store.conn.execute('SELECT id,account_id,video_id,played_at,is_generated FROM play_events WHERE id=9').fetchone()) == (
        9, 3, 'original-video', 100, None)
    assert list(map(tuple, store.conn.execute('SELECT p.id,p.raw_identity_key,p.video_id,p.played_at,s.source,p.is_generated '
        'FROM play_events p JOIN play_event_sources s ON s.event_id=p.id WHERE p.id<=34 ORDER BY p.id'))) == restored['raw_events']
    assert list(map(tuple, store.conn.execute('SELECT identity_key,recording_id FROM play_event_subjects WHERE id>=30 AND id<=34 ORDER BY id'))) == [
        ('song|artist', 1)] * 4 + [('@unresolved-video:unknown', None)]
    assert store.rec_model.weight_records()['genre:house'] == {'weight': 1.4, 'updated_at': 1.}
    assert not store.rec_model.get_rec_vectors()


def test_repeated_current_admission_keeps_repository_identity_and_every_sql_row(restored):
    store = restored['store']
    repo = store.tracks
    before = list(store.conn.iterdump())
    store.init_schema()
    assert store.tracks is repo
    assert list(store.conn.iterdump()) == before
    assert store.conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_real_reopen_preserves_all_early_rows_and_repaired_generated_policy(restored):
    before = list(restored['conn'].iterdump())
    store = Store(restored['path'])
    with closing(store.conn):
        store.init_schema()
        assert list(store.conn.iterdump()) == before
        with pytest.raises(sqlite3.IntegrityError, match='CHECK constraint failed'):
            store.conn.execute('UPDATE play_events SET is_generated=2')
        store.conn.rollback()
        assert list(store.conn.iterdump()) == before
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
