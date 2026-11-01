"""The current schema is complete without replaying the historical baseline."""
import hashlib
from contextlib import closing
from pathlib import Path
import sqlite3
import threading

import pytest

from yt_playlist.core.migrations import Migration, MigrationError, migrate
from yt_playlist.core.migrations import current, registry, v001_baseline

# The checked-in bootstrap includes the entire production registry.
LATEST_VERSION = registry.MIGRATIONS[-1].version
PREDECESSOR_VERSIONS = (1, 7, 8, 10, 11, 15, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27,
                       28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42,
                       48, 51, 52, 53, 59, 60, 61, 62, 63, 69, 70, 71, 72)


def connect(path=':memory:'):
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def objects(conn):
    return {name: (kind, current._table(conn, name, sql) if kind == 'table' else current._tokens(sql))
            for name, kind, sql in conn.execute(
                "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")}


def replay(conn):
    """Explicit historical replay, followed by independent regular admission."""
    migrate(conn, registry=registry.MIGRATIONS)
    migrate(conn)


@pytest.fixture(scope='module')
def predecessor_templates(tmp_path_factory):
    """Build each genuine empty SQL predecessor once, in historical order.

    SQLite backup pins a complete database, including any WAL contents. Every
    case still owns its database, inserts its historical evidence and executes
    all of the upgrades after its own predecessor.
    """
    root = tmp_path_factory.mktemp('schema-predecessors')
    paths = {}
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(root / 'app-home'))
        with closing(connect(root / 'chain.db')) as chain:
            for version in PREDECESSOR_VERSIONS:
                migrate(chain, registry=registry.MIGRATIONS[:version])
                path = root / f'v{version}.db'
                with closing(connect(path)) as snapshot:
                    chain.backup(snapshot)
                paths[version] = path
    return paths


@pytest.fixture(scope='module')
def current_contract(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('current-contract')))
        with closing(connect()) as fresh:
            migrate(fresh)
            return objects(fresh)


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


@pytest.mark.parametrize('version', [66, 76, 77, 78, 79])
def test_predecessor_validation_checks_all_foreign_keys_once(version):
    from yt_playlist.core.migrations.runner import validate_version

    conn = connect()
    try:
        migrate(conn, registry=registry.MIGRATIONS[:version])
        traced = []
        conn.set_trace_callback(traced.append)
        validate_version(conn, version)
        assert [sql for sql in traced if sql.lower() == 'pragma foreign_key_check'] == [
            'PRAGMA foreign_key_check']
        conn.execute('PRAGMA foreign_keys=OFF')
        with conn:
            conn.execute("INSERT INTO youtube_items(video_id,recording_id) VALUES ('broken',999999)")
        conn.execute('PRAGMA foreign_keys=ON')
        with pytest.raises(MigrationError, match='foreign-key reference'):
            validate_version(conn, version)
    finally:
        conn.close()


def test_fresh_and_current_do_not_call_historical_bootstrap_or_validation(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('historical schema used by current database')
    monkeypatch.setattr(v001_baseline, 'expected', forbidden)
    monkeypatch.setattr(v001_baseline, 'validate', forbidden)
    from yt_playlist.core.migrations import runner
    monkeypatch.setattr(runner, '_predecessor_contract', forbidden)
    # Forbid every upgrade callback, including additions after the old v51
    # bootstrap. A fresh install must use only the complete current definition.
    for migration in registry.MIGRATIONS:
        module = __import__(migration.apply.__module__, fromlist=['statements'])
        if hasattr(module, 'statements'):
            monkeypatch.setattr(module, 'statements', forbidden)
    monkeypatch.setattr(registry, 'MIGRATIONS', tuple(
        Migration(m.version, m.name, forbidden) for m in registry.MIGRATIONS))
    conn = connect()
    migrate(conn)
    conn.execute("INSERT INTO settings VALUES ('personal-setting','retain')")
    conn.commit()
    before = list(conn.iterdump())
    migrate(conn)
    assert list(conn.iterdump()) == before
    assert [tuple(row)[:2] for row in conn.execute('SELECT * FROM schema_migrations ORDER BY version')] == [
        (migration.version, migration.name) for migration in registry.MIGRATIONS]


@pytest.mark.parametrize('name', ['rec_mood', 'audio_vibe_feedback', 'audio_vibe_labels', 'audio_session_labels',
                                 'rec_vectors', 'rec_content_vectors', 'rec_discovered_content_vectors',
                                 'rec_artist_vectors', 'rec_artist_content_vectors', 'rec_prepared_content',
                                 'rec_serving_policies', 'rec_policy_bases', 'rec_policy_variants',
                                 'rec_group_generations', 'rec_group_identities', 'rec_group_definitions',
                                 'rec_group_members', 'rec_group_unknown', 'rec_group_lineage',
                                 'rec_group_head', 'rec_group_publications', 'rec_group_builds',
                                 'rec_group_graph', 'rec_group_assignments',
                                 'rec_proposal_cache_heads', 'rec_proposal_cache_chunks', 'rec_proposal_cache_builds'])
def test_current_restart_rejects_competing_retired_owners(name):
    with connect() as conn:
        migrate(conn)
        conn.execute(f'CREATE TABLE {name}(original_evidence TEXT)')
        conn.execute(f'INSERT INTO {name} VALUES (?)', ('retained original',))
        conn.commit()
        before = conn.total_changes
        with pytest.raises(MigrationError, match='Superseded current owner remains: ' + name):
            migrate(conn)
        assert conn.total_changes == before
        assert conn.execute(f'SELECT original_evidence FROM {name}').fetchone()[0] == 'retained original'


@pytest.mark.parametrize('version', PREDECESSOR_VERSIONS)
def test_explicit_predecessor_replay_matches_current_contract(
        tmp_path, version, predecessor_templates, current_contract):
    with closing(connect(tmp_path / 'upgrade.db')) as conn:
        source = predecessor_templates[version].resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(source, uri=True)) as predecessor:
            predecessor.backup(conn)
        populate(conn, version)
        replay(conn)
        current.validate(conn)
        assert objects(conn) == current_contract
        for key in current.SEED_SETTINGS:
            assert conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone() is not None
        assert conn.execute("SELECT value FROM settings WHERE key='personal-setting'").fetchone()[0] == 'retain'
        assert tuple(conn.execute('SELECT id,title,artist FROM tracks').fetchone()) == (17, 'Song', 'Artist')
        assert conn.execute('SELECT COUNT(*) FROM listening_observations').fetchone()[0] == 1
        before = list(conn.iterdump())
        migrate(conn)
        assert list(conn.iterdump()) == before
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not (tmp_path / 'backups').exists()


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
    fields = [row[1] for row in conn.execute('PRAGMA table_info(listening_observations)')]
    evidence = list(map(tuple, conn.execute('SELECT * FROM listening_observations')))
    replay(conn)
    projection = ','.join('account_id' if field == 'identity_id' else field for field in fields)
    assert list(map(tuple, conn.execute('SELECT '+projection+' FROM listening_observation_subjects'))) == evidence
    assert tuple(conn.execute('SELECT * FROM listening_day_counts').fetchone()) == ('song|artist', 1)
    assert conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == LATEST_VERSION
    before = list(conn.iterdump())
    migrate(conn)
    assert list(conn.iterdump()) == before
    conn.close()


def test_unversioned_early_schema_matches_fresh(tmp_path):
    with connect(tmp_path / 'legacy.db') as conn:
        conn.executescript((Path(__file__).parents[1] / 'fixtures/migrations/early_core.sql').read_text())
        replay(conn)
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
        replay(conn)
        assert tuple(conn.execute('SELECT id,account_id,video_id,played_at,is_generated FROM play_events').fetchone()) == (
            9, 3, 'original-video', 100, None)
        with pytest.raises(sqlite3.IntegrityError, match='CHECK constraint failed'):
            conn.execute('UPDATE play_events SET is_generated=2')


def test_initialized_state_matches_full_historical_chain():
    with connect() as upgraded, connect() as fresh:
        migrate(upgraded, registry=registry.MIGRATIONS)
        migrate(upgraded)
        migrate(fresh)
        assert objects(upgraded) == objects(fresh)
        for table, (kind, _) in objects(upgraded).items():
            if kind == 'table' and table not in ('schema_migrations', 'schema_changes'):
                assert sorted(map(tuple, upgraded.execute(f'SELECT * FROM "{table}"')), key=repr) == sorted(
                    map(tuple, fresh.execute(f'SELECT * FROM "{table}"')), key=repr), table
        assert list(map(tuple, upgraded.execute('SELECT version,name FROM schema_changes ORDER BY version'))) == list(
            map(tuple, fresh.execute('SELECT version,name FROM schema_changes ORDER BY version')))


def test_existing_store_rebinds_owners_after_upgrade_and_both_memory_readers_use_them():
    from yt_playlist.core.store import Store
    from yt_playlist.repos.compact_taste_groups import CompactTasteGroupsRepo
    store = Store(':memory:')
    try:
        migrate(store.conn, registry=registry.MIGRATIONS[:69])
        store._bind_repositories()
        assert not store._compact_group_files
        migrate(store.conn, registry=registry.MIGRATIONS)
        store.init_schema()
        assert isinstance(store.taste_groups, CompactTasteGroupsRepo)
        assert store._paid_descriptions and store._recommendation_files
        with store.read_view() as reader:
            assert isinstance(reader.taste_groups, CompactTasteGroupsRepo)
            assert reader._paid_descriptions and reader._recommendation_files
            assert reader.taste_groups.head() is None
        with store.read_snapshot() as snapshot:
            assert isinstance(snapshot.store.taste_groups, CompactTasteGroupsRepo)
            assert snapshot.store._paid_descriptions and snapshot.store._recommendation_files
            assert snapshot.store.taste_groups.head() is None
        repo = store.taste_groups
        store.init_schema()
        assert store.taste_groups is repo
    finally:
        store.conn.close()


@pytest.mark.parametrize('missing_lookup', [False, True])
def test_installed_v64_with_earlier_group_definition_gets_library_lookup(missing_lookup):
    with connect() as conn:
        migrate(conn, registry=registry.MIGRATIONS[:64])
        populate(conn, 64)
        if missing_lookup:
            conn.execute('DROP INDEX ix_group_library_inputs')
            conn.commit()
        fields = ','.join(row['name'] for row in conn.execute('PRAGMA table_info(recordings)'))
        before = list(map(tuple, conn.execute('SELECT * FROM recordings')))
        replay(conn)
        from yt_playlist.core.genre_classifications import expression
        projected = ','.join(expression('r')+' AS genre' if field == 'genre' else 'r.'+field
                             for field in fields.split(','))
        assert list(map(tuple, conn.execute('SELECT '+projected+' FROM recordings r'))) == before
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='ix_group_library_inputs'").fetchone()
        current.validate(conn)
        with connect() as fresh:
            migrate(fresh)
            assert objects(conn) == objects(fresh)


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
        replay(conn)
        projected = ','.join('s.source' if f=='source' else 'p.account_id' if f=='identity_id' else 'p.'+f for f in fields.split(','))
        assert list(map(tuple, conn.execute(f'SELECT {projected} FROM play_event_subjects p JOIN play_event_sources s ON s.event_id=p.id ORDER BY p.id'))) == events
        for table, rows in evidence.items():
            assert list(map(tuple, conn.execute(f'SELECT * FROM {table}'))) == rows
        current.validate(conn)
        with pytest.raises(sqlite3.IntegrityError, match='CHECK constraint failed'):
            conn.execute('UPDATE play_events SET is_generated=2')
        conn.rollback()
        before = list(conn.iterdump())
        migrate(conn)
        assert list(conn.iterdump()) == before


def test_policy_repair_failure_restores_column_data_and_history_without_backups(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    with connect(path) as conn:
        policy_predecessor(conn)
        conn.execute("INSERT INTO play_events(identity_id,identity_key,played_at,is_generated) VALUES (1,'song|artist',1,1)")
        conn.commit()
        before = list(conn.iterdump())
        original = registry.MIGRATIONS
        def interrupted(connection, context):
            original[31].apply(connection, context)
            raise RuntimeError('interrupted after policy replacement')
        monkeypatch.setattr(registry, 'MIGRATIONS', (*original[:31], Migration(32, original[31].name, interrupted)))
        with pytest.raises(MigrationError, match='interrupted after policy replacement'):
            migrate(conn, registry=registry.MIGRATIONS)
        assert list(conn.iterdump()) == before
        assert not (tmp_path / 'backups').exists()
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        monkeypatch.setattr(registry, 'MIGRATIONS', original)
        replay(conn)
        current.validate(conn)


def test_policy_repair_refuses_invalid_values_without_discarding_them():
    with connect() as conn:
        policy_predecessor(conn)
        conn.execute("INSERT INTO play_events(identity_id,identity_key,played_at,is_generated) VALUES (1,'song|artist',1,2)")
        conn.commit()
        before = list(conn.iterdump())
        with pytest.raises(MigrationError, match='Invalid generated-play policy'):
            migrate(conn, registry=registry.MIGRATIONS)
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
    with connect() as conn:
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
            replay(conn)
        else:
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
        with pytest.raises(MigrationError, match='injected bootstrap failure'):
            migrate(conn)
        assert list(conn.execute('SELECT * FROM sqlite_master')) == []
        monkeypatch.setattr(current, 'validate', validate)
        migrate(conn)
        current.validate(conn)


@pytest.mark.parametrize('upgraded', [False, True])
def test_explicit_later_migration_replay_applies_and_rolls_back(tmp_path, monkeypatch, upgraded):
    with connect(tmp_path / 'state.db') as conn:
        if upgraded:
            migrate(conn, registry=registry.MIGRATIONS[:30])
            populate(conn, 30)
            replay(conn)
        else:
            migrate(conn)
        original = registry.MIGRATIONS
        def interrupted(connection, context):
            connection.execute('CREATE TABLE later (id INTEGER PRIMARY KEY)')
            raise ValueError('injected later failure')
        monkeypatch.setattr(registry, 'MIGRATIONS', (*original, Migration(LATEST_VERSION + 1, 'later', interrupted)))
        with pytest.raises(MigrationError, match='injected later failure'):
            migrate(conn, registry=registry.MIGRATIONS)
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='later'").fetchone() is None
        assert conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == LATEST_VERSION
        monkeypatch.setattr(registry, 'MIGRATIONS', (*original, Migration(LATEST_VERSION + 1, 'later', lambda c, x:
            c.execute('CREATE TABLE later (id INTEGER PRIMARY KEY)'))))
        migrate(conn, registry=registry.MIGRATIONS)
        migrate(conn, registry=registry.MIGRATIONS)
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
