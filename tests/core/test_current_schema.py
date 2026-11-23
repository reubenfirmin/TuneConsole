"""The current schema is complete without replaying the historical baseline."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import runpy
import subprocess
import sys
import threading

import pytest

from yt_playlist.core.migrations import Migration, MigrationError, migrate
from yt_playlist.core.migrations import current, registry, regular


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These schema and startup checks do not construct an HTTP application."""

# The checked-in bootstrap includes the entire production registry.
LATEST_VERSION = registry.MIGRATIONS[-1].version


def connect(path=':memory:'):
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def objects(conn):
    return {name: (kind, current._table(conn, name, sql) if kind == 'table' else current._tokens(sql))
            for name, kind, sql in conn.execute(
                "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")}


@pytest.fixture(scope='module')
def current_template(tmp_path_factory):
    """Restart rejection cases share an immutable current database."""
    with closing(connect()) as template:
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('current-restart-template')))
            migrate(template)
        template.execute('PRAGMA query_only=ON')
        before = list(template.iterdump())
        yield template
        assert list(template.iterdump()) == before


@pytest.fixture
def current_database(current_template):
    with closing(connect()) as conn:
        current_template.backup(conn)
        yield conn


def assert_fresh_settings(conn):
    from yt_playlist.core import association_work, credit_retention, mode_retention
    from yt_playlist.core.metadata_contract import ACTIVE, EXPIRY
    from yt_playlist.core.migrations.preparation_checkpoint_storage import SEEDS

    settings = dict(conn.execute('SELECT key,value FROM settings'))
    assert settings.pop(ACTIVE) == '0'
    assert settings.pop(association_work.SERIAL) == '0'
    assert json.loads(settings.pop(EXPIRY)) == {'gc_cursor': 0, 'gc_after': 0, 'gc_shard': 0}
    assert settings.pop(mode_retention.HIGH_WATER) == '0'
    assert json.loads(settings.pop(mode_retention.CHECKPOINT)) == {'enabled': 0, 'cursor': 0, 'next_scan_at': 0}
    assert json.loads(settings.pop(credit_retention.CHECKPOINT)) == {
        'enabled': 0, 'cursor': 0, 'completed': 0, 'next_scan_at': 0}
    for key in ('playlist_recipe_id_high_water', 'playlist_canvas_id_high_water',
                'home_display_files_id_high_water', 'listening_output_files_id_high_water',
                'choice_learning_id_high_water', 'content_pending_serial', 'prepared_home_id_high_water',
                'prepared_listening_id_high_water', 'prepared_report_id_high_water'):
        assert settings.pop(key) == '0'
    assert settings == current.SEED_SETTINGS | SEEDS


def _forbid_historical_schema(monkeypatch):
    from yt_playlist.core.migrations import schema_definition

    def forbidden(*args, **kwargs):
        raise AssertionError('historical schema used by current database')

    schema_definition.contract.cache_clear()
    schema_definition._vibe_score_contract.cache_clear()
    schema_definition._reference_contract.cache_clear()
    # Forbid every upgrade callback, including additions after the old v51
    # bootstrap. A fresh install must use only the complete current definition.
    monkeypatch.setattr(registry, 'MIGRATIONS', tuple(
        Migration(m.version, m.name, forbidden) for m in registry.MIGRATIONS))
    monkeypatch.setattr(regular, 'CHANGES', tuple(
        regular.Change(c.version, c.name, forbidden, c.validate) for c in regular.CHANGES))


def test_fresh_and_current_do_not_call_historical_bootstrap_or_validation(monkeypatch, tmp_path):
    from yt_playlist.core.migrations import schema_definition

    # A warm process can hide import-time historical DDL behind cached jobs.
    # Exercise both admissions with the same guard in independent cold processes.
    cold = '''
import sys
from pathlib import Path
root, home = map(Path, sys.argv[1:3])
sys.path[:0] = [str(root), str(root / 'src')]
import os
os.environ['YT_PLAYLIST_HOME'] = str(home)
import pytest
from tests.core.test_current_schema import _forbid_historical_schema
from yt_playlist.core.store import Store
with pytest.MonkeyPatch.context() as patch:
    _forbid_historical_schema(patch)
    assert 'yt_playlist.core.preparation.model_trials' not in sys.modules
    store = Store(home / 'current.db')
    try:
        store.init_schema()
        store.settings.set_setting('cold-current', 'persisted é')
        assert store.settings.get_setting('cold-current') == 'persisted é'
        before = list(store.conn.iterdump())
        store.init_schema()
        assert list(store.conn.iterdump()) == before
    finally:
        store.conn.close()
'''
    cold_home = tmp_path / 'cold-home'
    cold_home.mkdir()
    for admission in ('fresh', 'current reopen'):
        result = subprocess.run(
            [sys.executable, '-I', '-c', cold, str(Path(__file__).parents[2]), str(cold_home)],
            cwd=tmp_path, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, (admission, result.stdout, result.stderr)

    # Compare the maintained source export before forbidding every generator.
    # This reads/generates text only; the actual bootstrap below owns its DB.
    exporter = runpy.run_path(Path(__file__).parents[2] / 'scripts/export-current-schema.py')
    with monkeypatch.context() as text_only:
        text_only.setattr(sqlite3, 'connect', lambda *a, **kw: pytest.fail('Exporter opened a database'))
        for path, expected in exporter['render'](schema_definition).items():
            assert path.read_text(encoding='utf-8') == expected

    _forbid_historical_schema(monkeypatch)
    path = tmp_path / 'complete-current.db'
    with closing(connect(path)) as conn:
        from yt_playlist.core import credit_retention
        from yt_playlist.core.migrations import control_settings_storage, preparation_checkpoint_storage

        migrate(conn)
        retired = control_settings_storage.RETIRED + preparation_checkpoint_storage.RETIRED
        assert set(retired).isdisjoint(name for name, in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type='table'"))
        assert_fresh_settings(conn)
        assert credit_retention.state(conn) == dict(enabled=0, cursor=0, completed=0, next_scan_at=0.0)
        assert tuple(conn.execute('SELECT position,group_key,revision,cursor FROM rec_vibe_pending WHERE position>0'
                                  ).fetchone()) == (
            1, '*', 2, '')
        assert conn.execute("SELECT 1 FROM genre_catalog WHERE name='Techno'").fetchone()
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE type='index' AND name='ix_genre_catalog_name'").fetchone()
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE type='view' AND name='listening_days'").fetchone()
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE type='trigger' AND name='playlists_scope_insert'").fetchone()
        from yt_playlist.core import vibe_versions
        assert vibe_versions.normalized(conn)
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        conn.execute("INSERT INTO accounts(id,label,is_master) VALUES (1,'Main',1)")
        with pytest.raises(sqlite3.IntegrityError, match='CHECK constraint failed'):
            conn.execute("UPDATE accounts SET listening_timezone='UTC',listening_timezone_confirmed=2 WHERE id=1")
        conn.rollback()
        conn.execute("INSERT INTO settings VALUES ('personal-setting','retain')")
        conn.commit()
        before = list(conn.iterdump())
        migrate(conn)
        assert list(conn.iterdump()) == before
        assert [tuple(row)[:2] for row in conn.execute('SELECT * FROM schema_migrations ORDER BY version')] == [
            (migration.version, migration.name) for migration in registry.MIGRATIONS]
    with closing(connect(path)) as reopened:
        migrate(reopened)
        assert list(reopened.iterdump()) == before


@pytest.mark.parametrize('name', ['rec_mood', 'audio_vibe_feedback', 'audio_vibe_labels', 'audio_session_labels',
                                 'rec_vectors', 'rec_content_vectors', 'rec_discovered_content_vectors',
                                 'rec_artist_vectors', 'rec_artist_content_vectors', 'rec_prepared_content',
                                 'rec_serving_policies', 'rec_policy_bases', 'rec_policy_variants',
                                 'rec_group_generations', 'rec_group_identities', 'rec_group_definitions',
                                 'rec_group_members', 'rec_group_unknown', 'rec_group_lineage',
                                 'rec_group_head', 'rec_group_publications', 'rec_group_builds',
                                 'rec_group_graph', 'rec_group_assignments',
                                 'rec_proposal_cache_heads', 'rec_proposal_cache_chunks', 'rec_proposal_cache_builds'])
def test_current_restart_rejects_competing_retired_owners(name, current_database):
    conn = current_database
    with conn:
        conn.execute(f'CREATE TABLE {name}(original_evidence TEXT)')
        conn.execute(f'INSERT INTO {name} VALUES (?)', ('retained original',))
        conn.commit()
        before = conn.total_changes
        with pytest.raises(MigrationError, match='Superseded current owner remains: ' + name):
            migrate(conn)
        assert conn.total_changes == before
        assert conn.execute(f'SELECT original_evidence FROM {name}').fetchone()[0] == 'retained original'


def test_admission_binds_current_owners_and_both_memory_readers_use_them():
    from tests.store_fixture_support import bounded_consumer_schema
    from yt_playlist.core.store import Store
    from yt_playlist.controllers.compact_taste_groups import CompactTasteGroups
    from yt_playlist.repos.compact_taste_groups import CompactTasteGroupsRepo
    store = Store(':memory:')
    try:
        assert not hasattr(store, 'taste_groups')
        with bounded_consumer_schema((('jazz', 'Jazz'),), snapshot_guards=False) as completed:
            completed.backup(store.conn)
        store.init_schema()
        assert isinstance(store.taste_groups, CompactTasteGroups)
        assert isinstance(store.taste_groups.repository, CompactTasteGroupsRepo)
        assert store.taste_groups.head() is None
        assert store.album_descriptions.summary() == (0, False)
        assert store.recommendations.head('home') is None
        with store.read_view() as reader:
            assert isinstance(reader.taste_groups, CompactTasteGroups)
            assert isinstance(reader.taste_groups.repository, CompactTasteGroupsRepo)
            assert reader.album_descriptions.summary() == (0, False)
            assert reader.recommendations.head('home') is None
            assert reader.taste_groups.head() is None
        with store.read_snapshot() as snapshot:
            assert isinstance(snapshot.store.taste_groups, CompactTasteGroups)
            assert isinstance(snapshot.store.taste_groups.repository, CompactTasteGroupsRepo)
            assert snapshot.store.album_descriptions.summary() == (0, False)
            assert snapshot.store.recommendations.head('home') is None
            assert snapshot.store.taste_groups.head() is None
        repo = store.taste_groups
        store.init_schema()
        assert store.taste_groups is repo
    finally:
        store.conn.close()


@pytest.mark.parametrize('sql', [
    'DROP INDEX ix_play_event_account_time',
    'DROP VIEW listening_days',
    'DROP TRIGGER playlists_scope_insert',
    'DROP INDEX ix_album_feedback_queued_response',
    "DELETE FROM settings WHERE key='catalog_identity_version'",
    "UPDATE settings SET value='99' WHERE key='catalog_identity_version'",
    "UPDATE schema_migrations SET name='wrong' WHERE version=31",
    "DELETE FROM schema_migrations WHERE version=15",
    f"INSERT INTO schema_migrations VALUES ({LATEST_VERSION + 1},'future',1)",
    f"INSERT INTO schema_changes VALUES ({len(regular.CHANGES) + 1},'future',1)",
    'DROP VIEW active_playlists',
])
def test_malformed_current_database_is_rejected_without_repair(sql, current_database):
    conn = current_database
    with conn:
        conn.execute(sql)
        conn.commit()
        before = list(conn.iterdump())
        with pytest.raises(MigrationError):
            migrate(conn)
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize('old,new,owner', [
    ('CHECK(confirmed IN (0,1))', 'CHECK(confirmed IN (0,1,2))', 'listening_calendars'),
    ('FOREIGN KEY(video_id,recording_id) REFERENCES youtube_items(video_id,recording_id) DEFERRABLE INITIALLY DEFERRED',
     'FOREIGN KEY(video_id,recording_id) REFERENCES youtube_items(video_id,recording_id)', 'catalog_entries'),
    ('CREATE INDEX ix_genre_catalog_name ON genre_catalog(name COLLATE NOCASE)',
     'CREATE INDEX ix_genre_catalog_name ON genre_catalog(name)', 'ix_genre_catalog_name'),
    ('id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT,artist TEXT,year TEXT',
     'id INTEGER PRIMARY KEY,title TEXT,artist TEXT,year TEXT', 'albums'),
])
def test_current_constraints_are_validated(old, new, owner):
    with closing(connect()) as conn, conn:
        source = ';\n'.join(current.statements())+';'
        assert old in source
        conn.executescript(source.replace(old, new))
        conn.executemany('INSERT INTO settings VALUES (?,?)', current.SEED_SETTINGS.items())
        with pytest.raises(MigrationError, match=owner):
            current.validate(conn)


def test_deferral_on_another_foreign_key_is_rejected_before_catalog_use():
    source = ';\n'.join(current.statements())+';'
    catalog_table = next(sql for sql in current.statements() if sql.startswith('CREATE TABLE catalog_entries '))
    composite = 'REFERENCES youtube_items(video_id,recording_id) DEFERRABLE INITIALLY DEFERRED'
    inline = ('recording_id INTEGER NOT NULL REFERENCES recordings(id),'
              'video_id TEXT UNIQUE REFERENCES youtube_items(video_id)')
    assert catalog_table.count(composite) == catalog_table.count(inline) == 1
    malformed_table = catalog_table.replace(composite, 'REFERENCES youtube_items(video_id,recording_id)').replace(
        inline, inline.replace('REFERENCES recordings(id)',
                               'REFERENCES recordings(id) DEFERRABLE INITIALLY DEFERRED'))
    malformed = source.replace(catalog_table, malformed_table)
    with closing(connect()) as conn, conn:
        conn.executescript(malformed)
        conn.executemany('INSERT INTO settings VALUES (?,?)', current.SEED_SETTINGS.items())
        with pytest.raises(MigrationError, match='catalog_entries'):
            current.validate(conn)


def test_validated_current_catalog_allows_verified_reassociation():
    from yt_playlist.core import catalog

    with connect() as conn:
        migrate(conn)
        first = catalog.upsert(conn, 'first', 'First', 'Artist')
        second = catalog.upsert(conn, 'second', 'Second', 'Artist')
        target = conn.execute('SELECT recording_id FROM track_sources WHERE id=?', (second,)).fetchone()[0]
        conn.commit()
        current.validate(conn)
        catalog.associate_item(conn, 'first', target, source='verified', evidence={'review': 'same recording'}, now=1)
        conn.commit()
        assert conn.execute('SELECT recording_id FROM track_sources WHERE id=?', (first,)).fetchone()[0] == target
        assert conn.execute("SELECT recording_id FROM track_sources WHERE video_id='first'").fetchone()[0] == target
        current.validate(conn)
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def test_composite_foreign_key_order_and_grouping_are_validated():
    with closing(connect()) as conn, conn:
        source = ';\n'.join(current.statements())+';'
        catalog_table = next(sql for sql in current.statements() if sql.startswith('CREATE TABLE catalog_entries '))
        original = 'FOREIGN KEY(video_id,recording_id) REFERENCES youtube_items(video_id,recording_id)'
        # Swapping whole pairs preserves the flattened pragma rows but changes
        # the declared composite constraint's order.
        changed = 'FOREIGN KEY(recording_id,video_id) REFERENCES youtube_items(recording_id,video_id)'
        assert catalog_table.count(original) == 1
        conn.executescript(source.replace(catalog_table, catalog_table.replace(original, changed)))
        conn.executemany('INSERT INTO settings VALUES (?,?)', current.SEED_SETTINGS.items())
        with pytest.raises(MigrationError, match='catalog_entries'):
            current.validate(conn)


def test_bootstrap_failure_rolls_back_all_objects_and_history(monkeypatch):
    validate = current.validate
    monkeypatch.setattr(current, 'validate', lambda conn: (_ for _ in ()).throw(ValueError('injected bootstrap failure')))
    with connect() as conn:
        with pytest.raises(ValueError, match='injected bootstrap failure'):
            migrate(conn)
        assert list(conn.execute('SELECT * FROM sqlite_master')) == []
        monkeypatch.setattr(current, 'validate', validate)
        migrate(conn)
        current.validate(conn)


def test_concurrent_current_bootstraps_create_one_history(tmp_path):
    path = tmp_path / 'state.db'
    barrier = threading.Barrier(2)
    errors = []
    def startup():
        try:
            with connect(path) as conn:
                barrier.wait()
                migrate(conn)
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=startup) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    with connect(path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0] == LATEST_VERSION
