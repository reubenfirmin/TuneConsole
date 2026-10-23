"""The current schema is complete without replaying the historical baseline."""
import hashlib
from pathlib import Path
import sqlite3
import threading

import pytest

from yt_playlist.core.migrations import Migration, MigrationError, migrate
from yt_playlist.core.migrations import current, registry, v001_baseline

# The bootstrap contract can precede later data-only or additive migrations.
# A completed migrate() records the full registry, including that later tail.
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


def populate(conn, version):
    conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (1,'Main','bridge',1)")
    if version < 8:
        conn.execute("INSERT INTO tracks(id,video_id,title,artist,identity_key) VALUES (17,'song','Song','Artist','song|artist')")
    else:
        conn.execute("INSERT INTO recordings(id,identity_key,title,artist) VALUES (17,'song|artist','Song','Artist')")
        conn.execute("INSERT INTO youtube_items(video_id,recording_id,title,artist) VALUES ('song',17,'Song','Artist')")
        conn.execute("INSERT INTO catalog_entries(id,recording_id,video_id) VALUES (17,17,'song')")
        conn.execute("INSERT INTO library_memberships VALUES (17,'fixture',NULL)")
    if version < 33:
        conn.execute("INSERT INTO history_snapshots VALUES (1,1,100)")
        conn.execute("INSERT INTO history_items(snapshot_id,identity_key) VALUES (1,'song|artist')")
    else:
        from yt_playlist.core import listening
        listening.observe_day(conn, 1, 'song|artist', '1970-01-01', 'independent', 'UTC', 100)
    conn.execute("INSERT INTO settings VALUES ('personal-setting','retain')")
    conn.commit()


def policy_predecessor(conn):
    # Independent pre-numbered layout: reproduces v001's missing CHECK rather
    # than forging an old migration stamp on today's schema declaration.
    conn.execute('CREATE TABLE play_events (id INTEGER PRIMARY KEY,identity_id INTEGER NOT NULL REFERENCES identities(id),'
                 'identity_key TEXT NOT NULL,video_id TEXT,played_at REAL NOT NULL,playlist_ytm_id TEXT,like_status TEXT)')
    migrate(conn, registry=registry.MIGRATIONS[:31])
    populate(conn, 31)


def test_fresh_and_current_do_not_call_historical_bootstrap_or_validation(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('historical schema used by current database')
    monkeypatch.setattr(v001_baseline, 'expected', forbidden)
    monkeypatch.setattr(v001_baseline, 'validate', forbidden)
    conn = connect()
    migrate(conn)
    conn.execute("INSERT INTO settings VALUES ('personal-setting','retain')")
    conn.commit()
    before = list(conn.iterdump())
    migrate(conn)
    assert list(conn.iterdump()) == before
    assert [tuple(row)[:2] for row in conn.execute('SELECT * FROM schema_migrations ORDER BY version')] == [
        (migration.version, migration.name) for migration in registry.MIGRATIONS]


@pytest.mark.parametrize('version', [1, 7, 8, 10, 11, 15, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42])
def test_every_supported_predecessor_family_matches_current_contract(tmp_path, version):
    with connect(tmp_path / 'upgrade.db') as conn:
        migrate(conn, registry=registry.MIGRATIONS[:version])
        populate(conn, version)
        migrate(conn)
        current.validate(conn)
        with connect() as fresh:
            migrate(fresh)
            assert objects(conn) == objects(fresh)
            for key in current.SEED_SETTINGS:
                assert conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone() is not None
        assert conn.execute("SELECT value FROM settings WHERE key='personal-setting'").fetchone()[0] == 'retain'
        assert tuple(conn.execute('SELECT id,title,artist FROM tracks').fetchone()) == (17, 'Song', 'Artist')
        assert conn.execute('SELECT COUNT(*) FROM listening_observations').fetchone()[0] == 1
        before = list(conn.iterdump())
        migrate(conn)
        assert list(conn.iterdump()) == before
    backups = list((tmp_path / 'backups').glob('*.db'))
    assert len(backups) == any(m.replaces_data for m in registry.MIGRATIONS[version:])
    if backups:
        with connect(backups[0]) as backup:
            assert backup.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            # Additive versions may commit before the first destructive step.
            backup_version = next(m.version - 1 for m in registry.MIGRATIONS[version:] if m.replaces_data)
            assert backup.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == backup_version
            assert backup.execute("SELECT value FROM settings WHERE key='personal-setting'").fetchone()[0] == 'retain'


@pytest.mark.parametrize('has_counts_view', [False, True])
def test_v037_installed_during_home_work_upgrades_without_losing_evidence(has_counts_view, monkeypatch):
    monkeypatch.setenv('TZ', 'UTC')
    conn = connect()
    migrate(conn, registry=registry.MIGRATIONS[:37])
    populate(conn, 37)
    if has_counts_view:
        conn.execute('CREATE VIEW listening_day_counts AS SELECT identity_key,COUNT(*) plays '
                     'FROM (SELECT DISTINCT identity_id,identity_key,day FROM eligible_listening_evidence) '
                     'GROUP BY identity_key')
        conn.commit()
    evidence = list(map(tuple, conn.execute('SELECT * FROM listening_observations')))
    migrate(conn)
    assert list(map(tuple, conn.execute('SELECT * FROM listening_observations'))) == evidence
    assert tuple(conn.execute('SELECT * FROM listening_day_counts').fetchone()) == ('song|artist', 1)
    assert conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == LATEST_VERSION
    before = list(conn.iterdump())
    migrate(conn)
    assert list(conn.iterdump()) == before
    conn.close()


def test_unversioned_early_schema_matches_fresh(tmp_path):
    with connect(tmp_path / 'legacy.db') as conn:
        conn.executescript((Path(__file__).parents[1] / 'fixtures/migrations/early_core.sql').read_text())
        migrate(conn)
        with connect() as fresh:
            migrate(fresh)
            assert objects(conn) == objects(fresh)
        assert tuple(conn.execute('SELECT id,orig_title,orig_artist FROM tracks').fetchone()) == (
            17, 'Original title', 'Artist')


def test_preexisting_play_ledger_receives_constraint_missing_from_v001(tmp_path):
    with connect(tmp_path / 'legacy.db') as conn:
        conn.executescript((Path(__file__).parents[1] / 'fixtures/migrations/early_core.sql').read_text())
        conn.executescript('''
            CREATE TABLE play_events (id INTEGER PRIMARY KEY,identity_id INTEGER NOT NULL REFERENCES identities(id),
                identity_key TEXT NOT NULL,video_id TEXT,played_at REAL NOT NULL,playlist_ytm_id TEXT,like_status TEXT);
            INSERT INTO play_events VALUES (9,3,'original title|artist','original-video',100,NULL,NULL);
        ''')
        migrate(conn)
        assert tuple(conn.execute('SELECT id,identity_id,video_id,played_at,is_generated FROM play_events').fetchone()) == (
            9, 3, 'original-video', 100, None)
        with pytest.raises(sqlite3.IntegrityError, match='CHECK constraint failed'):
            conn.execute('UPDATE play_events SET is_generated=2')


def test_initialized_state_matches_full_historical_chain():
    with connect() as upgraded, connect() as fresh:
        migrate(upgraded, registry=registry.MIGRATIONS)
        migrate(fresh)
        assert objects(upgraded) == objects(fresh)
        assert list(map(tuple, upgraded.execute('SELECT * FROM settings ORDER BY key'))) == list(map(
            tuple, fresh.execute('SELECT * FROM settings ORDER BY key')))


def test_installed_optional_generated_column_is_migrated_with_all_event_evidence():
    with connect() as conn:
        policy_predecessor(conn)
        conn.executemany('INSERT INTO play_events(identity_id,identity_key,video_id,played_at,is_generated,source) '
                         "VALUES (1,'song|artist','song',?,?, 'live')", [(1, None), (2, 0), (3, 1)])
        conn.execute("INSERT INTO spotify_history_listens VALUES (1,'spotify:track:one',1,1,NULL)")
        conn.commit()
        fields = 'id,identity_id,identity_key,video_id,played_at,playlist_ytm_id,like_status,source,is_generated,recording_id,raw_identity_key'
        events = list(map(tuple, conn.execute(f'SELECT {fields} FROM play_events ORDER BY id')))
        evidence = {table: list(map(tuple, conn.execute(f'SELECT * FROM {table}'))) for table in (
            'play_event_calendar', 'play_event_sources', 'spotify_history_listens')}
        migrate(conn)
        assert list(map(tuple, conn.execute(f'SELECT {fields} FROM play_events ORDER BY id'))) == events
        for table, rows in evidence.items():
            assert list(map(tuple, conn.execute(f'SELECT * FROM {table}'))) == rows
        current.validate(conn)
        with pytest.raises(sqlite3.IntegrityError, match='CHECK constraint failed'):
            conn.execute('UPDATE play_events SET is_generated=2')
        conn.rollback()
        before = list(conn.iterdump())
        migrate(conn)
        assert list(conn.iterdump()) == before


def test_policy_repair_failure_restores_column_data_history_and_verified_backup(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    with connect(path) as conn:
        policy_predecessor(conn)
        conn.execute("INSERT INTO play_events(identity_id,identity_key,played_at,is_generated) VALUES (1,'song|artist',1,1)")
        conn.commit()
        before = list(conn.iterdump())
        prior_backups = set((tmp_path / 'backups').glob('*.db'))
        original = registry.MIGRATIONS
        def interrupted(connection, context):
            original[31].apply(connection, context)
            raise RuntimeError('interrupted after policy replacement')
        monkeypatch.setattr(registry, 'MIGRATIONS', (*original[:31], Migration(32, original[31].name, interrupted)))
        with pytest.raises(MigrationError, match='interrupted after policy replacement'):
            migrate(conn)
        assert list(conn.iterdump()) == before
        new_backups = set((tmp_path / 'backups').glob('*.db')) - prior_backups
        assert len(new_backups) == 1
        with connect(new_backups.pop()) as backup:
            assert list(backup.iterdump()) == before
            assert backup.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        monkeypatch.setattr(registry, 'MIGRATIONS', original)
        migrate(conn)
        current.validate(conn)


def test_policy_repair_refuses_invalid_values_without_discarding_them():
    with connect() as conn:
        policy_predecessor(conn)
        conn.execute("INSERT INTO play_events(identity_id,identity_key,played_at,is_generated) VALUES (1,'song|artist',1,2)")
        conn.commit()
        before = list(conn.iterdump())
        with pytest.raises(MigrationError, match='Invalid generated-play policy'):
            migrate(conn)
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize('sql', [
    'DROP INDEX ix_play_event_account_time',
    'DROP VIEW listening_days',
    'DROP TRIGGER playlists_scope_insert',
    "DELETE FROM settings WHERE key='catalog_identity_version'",
    "UPDATE settings SET value='99' WHERE key='catalog_identity_version'",
    "UPDATE schema_migrations SET name='wrong' WHERE version=31",
    "DELETE FROM schema_migrations WHERE version=15",
    f"INSERT INTO schema_migrations VALUES ({LATEST_VERSION + 1},'future',1)",
    'ALTER TABLE listening_calendars DROP COLUMN confirmed',
])
def test_malformed_current_database_is_rejected_without_repair(sql):
    with connect() as conn:
        migrate(conn)
        conn.execute(sql)
        conn.commit()
        before = list(conn.iterdump())
        with pytest.raises(MigrationError):
            migrate(conn)
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize('old,new', [
    ('CHECK(confirmed IN (0,1))', 'CHECK(confirmed IN (0,1,2))'),
    ('FOREIGN KEY(video_id,recording_id) REFERENCES youtube_items(video_id,recording_id) DEFERRABLE INITIALLY DEFERRED',
     'FOREIGN KEY(video_id,recording_id) REFERENCES youtube_items(video_id,recording_id)'),
    ('name TEXT PRIMARY KEY COLLATE NOCASE', 'name TEXT PRIMARY KEY'),
    ('id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT,artist TEXT,year TEXT',
     'id INTEGER PRIMARY KEY,title TEXT,artist TEXT,year TEXT'),
])
def test_current_constraints_are_validated(old, new):
    with connect() as conn:
        source = current.DEFINITION.read_text()
        assert old in source
        conn.executescript(source.replace(old, new))
        conn.executemany('INSERT INTO settings VALUES (?,?)', current.SEED_SETTINGS.items())
        with pytest.raises(MigrationError):
            current.validate(conn)


def test_deferral_on_another_foreign_key_is_rejected_before_catalog_use():
    source = current.DEFINITION.read_text()
    composite = 'REFERENCES youtube_items(video_id,recording_id) DEFERRABLE INITIALLY DEFERRED'
    inline = ('recording_id INTEGER NOT NULL REFERENCES recordings(id),'
              'video_id TEXT UNIQUE REFERENCES youtube_items(video_id)')
    assert source.count(composite) == source.count(inline) == 1
    malformed = source.replace(composite, 'REFERENCES youtube_items(video_id,recording_id)').replace(
        inline, inline.replace('REFERENCES recordings(id)',
                               'REFERENCES recordings(id) DEFERRABLE INITIALLY DEFERRED'))
    with connect() as conn:
        conn.executescript(malformed)
        conn.executemany('INSERT INTO settings VALUES (?,?)', current.SEED_SETTINGS.items())
        with pytest.raises(MigrationError, match='catalog_entries'):
            current.validate(conn)


@pytest.mark.parametrize('version', [None, 24, 31])
def test_validated_fresh_and_upgraded_catalogs_allow_verified_reassociation(version):
    from yt_playlist.core import catalog

    with connect() as conn:
        if version is not None:
            migrate(conn, registry=registry.MIGRATIONS[:version])
        migrate(conn)
        first = catalog.upsert(conn, 'first', 'First', 'Artist')
        second = catalog.upsert(conn, 'second', 'Second', 'Artist')
        target = conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (second,)).fetchone()[0]
        conn.commit()
        current.validate(conn)
        catalog.associate_item(conn, 'first', target, source='verified', evidence={'review': 'same recording'}, now=1)
        conn.commit()
        assert conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (first,)).fetchone()[0] == target
        assert conn.execute("SELECT recording_id FROM youtube_items WHERE video_id='first'").fetchone()[0] == target
        current.validate(conn)
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def test_composite_foreign_key_order_and_grouping_are_validated():
    with connect() as conn:
        source = current.DEFINITION.read_text()
        original = 'FOREIGN KEY(video_id,recording_id) REFERENCES youtube_items(video_id,recording_id)'
        # Swapping whole pairs preserves the flattened pragma rows but changes
        # the declared composite constraint's order.
        changed = 'FOREIGN KEY(recording_id,video_id) REFERENCES youtube_items(recording_id,video_id)'
        assert source.count(original) == 1
        conn.executescript(source.replace(original, changed))
        conn.executemany('INSERT INTO settings VALUES (?,?)', current.SEED_SETTINGS.items())
        with pytest.raises(MigrationError, match='catalog_entries'):
            current.validate(conn)


def test_bootstrap_failure_rolls_back_all_objects_and_history(monkeypatch):
    validate = current.validate
    monkeypatch.setattr(current, 'validate', lambda conn: (_ for _ in ()).throw(ValueError('injected bootstrap failure')))
    with connect() as conn:
        with pytest.raises(MigrationError, match='injected bootstrap failure'):
            migrate(conn)
        assert list(conn.execute('SELECT * FROM sqlite_master')) == []
        monkeypatch.setattr(current, 'validate', validate)
        migrate(conn)
        current.validate(conn)


@pytest.mark.parametrize('upgraded', [False, True])
def test_later_migration_applies_to_fresh_and_upgraded_and_rolls_back(tmp_path, monkeypatch, upgraded):
    with connect(tmp_path / 'state.db') as conn:
        if upgraded:
            migrate(conn, registry=registry.MIGRATIONS[:30])
            populate(conn, 30)
        migrate(conn)
        original = registry.MIGRATIONS
        def interrupted(connection, context):
            connection.execute('CREATE TABLE later (id INTEGER PRIMARY KEY)')
            raise ValueError('injected later failure')
        monkeypatch.setattr(registry, 'MIGRATIONS', (*original, Migration(LATEST_VERSION + 1, 'later', interrupted)))
        with pytest.raises(MigrationError, match='injected later failure'):
            migrate(conn)
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='later'").fetchone() is None
        assert conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == LATEST_VERSION
        monkeypatch.setattr(registry, 'MIGRATIONS', (*original, Migration(LATEST_VERSION + 1, 'later', lambda c, x:
            c.execute('CREATE TABLE later (id INTEGER PRIMARY KEY)'))))
        migrate(conn)
        migrate(conn)
        assert conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == LATEST_VERSION + 1
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='later'").fetchone()


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


def test_legacy_schema_resource_is_frozen_and_removed_module_stays_absent():
    directory = Path(current.__file__).parent
    assert not (directory / 'legacy_schema.py').exists()
    # Frozen v001 SQL resource, before isolation into the historical package.
    assert hashlib.sha256((directory / 'historical/v001_schema.py').read_bytes()).hexdigest() == (
        '99d179046151aca64dfed2ec5f235b76d3a4d3c50a328e886916ed1d9f439159')
