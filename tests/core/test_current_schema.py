"""The current schema is complete without replaying the historical baseline."""
import hashlib
import json
from collections import Counter
from contextlib import closing
from pathlib import Path
import sqlite3
import runpy
import subprocess
import sys
import threading
import time

import pytest

from yt_playlist.core.migrations import Migration, MigrationError, migrate
from yt_playlist.core.migrations import current, registry, regular, v001_baseline

# The checked-in bootstrap includes the entire production registry.
LATEST_VERSION = registry.MIGRATIONS[-1].version
PREDECESSOR_VERSIONS = (1, 7, 8, 10, 11, 15, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27,
                       28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42,
                       48, 51, 52, 53, 59, 60, 61, 62, 63, 69, 70, 71, 72)
FOREIGN_KEY_VERSIONS = (66, 76, 77, 78, 79, 85, 93, 103, 106)
# Keep independent raw-track, normalized-catalog and listening representations,
# plus the original first/middle/late real disk sessions. Every other installed
# start still gets its own populated admission below, without a repeated suffix.
FULL_REPLAY_VERSIONS = (1, 8, 24, 33, 72)


def connect(path=':memory:'):
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def objects(conn, *, canonical_versions=False):
    result = {name: (kind, current._table(conn, name, sql) if kind == 'table' else current._tokens(sql))
            for name, kind, sql in conn.execute(
                "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")}
    if canonical_versions:
        from yt_playlist.core import vibe_versions
        if not vibe_versions.normalized(conn):
            # Installed text identities remain writable until explicit rebuild.
            # These comparisons account only for that supported representation;
            # all other objects and constraints still compare exactly.
            result.update(current._schema_contract([
                vibe_versions.transform(conn.execute(
                    'SELECT sql FROM sqlite_schema WHERE name=?', (table,)).fetchone()[0], table)
                for table in vibe_versions.COLUMNS]))
    return result


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
            for version in sorted(set(PREDECESSOR_VERSIONS + FOREIGN_KEY_VERSIONS)):
                migrate(chain, registry=registry.MIGRATIONS[:version])
                from tests.prepared_fixture_support import coordinates
                assert not coordinates(chain), 'Empty predecessor retains external files'
                path = root / f'v{version}.db'
                with closing(connect(path)) as snapshot:
                    chain.backup(snapshot)
                paths[version] = path
    return paths


@pytest.fixture(scope='module')
def numbered_template(tmp_path_factory):
    """The real current bootstrap before independent regular admission."""
    with closing(connect()) as template:
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('numbered-template')))
            template.execute('BEGIN IMMEDIATE')
            current.bootstrap(template, registry.MIGRATIONS, 0.)
            template.commit()
        template.execute('PRAGMA query_only=ON')
        before = list(template.iterdump())
        yield template
        assert list(template.iterdump()) == before


@pytest.fixture(scope='module')
def current_template(numbered_template, tmp_path_factory):
    """Admit once; restart rejection cases and contract comparisons share SQL."""
    with closing(connect()) as template:
        numbered_template.backup(template)
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('current-restart-template')))
            migrate(template)
        template.execute('PRAGMA query_only=ON')
        before = list(template.iterdump())
        yield template
        assert list(template.iterdump()) == before


@pytest.fixture(scope='module')
def current_contract(current_template):
    return objects(current_template)


@pytest.fixture(scope='module')
def numbered_contract(numbered_template):
    return objects(numbered_template)


@pytest.fixture
def current_database(current_template):
    with closing(connect()) as conn:
        current_template.backup(conn)
        yield conn


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


@pytest.mark.parametrize('version', FOREIGN_KEY_VERSIONS)
def test_predecessor_validation_checks_all_foreign_keys_once(version, predecessor_templates):
    from yt_playlist.core.migrations.runner import validate_version

    conn = connect()
    try:
        source = predecessor_templates[version].resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(source, uri=True)) as predecessor:
            predecessor.backup(conn)
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


def _forbid_historical_schema(monkeypatch):
    from yt_playlist.core import (
        frozen_share_contract, genre_contract, schema_definition, schema_transforms)

    def forbidden(*args, **kwargs):
        raise AssertionError('historical schema used by current database')

    monkeypatch.setattr(schema_definition, 'assembled_statements', forbidden)
    monkeypatch.setattr(schema_definition, '_statements', forbidden)
    for name, operation in vars(schema_transforms).copy().items():
        if callable(operation) and getattr(operation, '__module__', None) == schema_transforms.__name__:
            monkeypatch.setattr(schema_transforms, name, forbidden)
    monkeypatch.setattr(genre_contract, 'classified_definition', forbidden)
    monkeypatch.setattr(frozen_share_contract, 'fences', forbidden)
    schema_definition.contract.cache_clear()
    schema_definition._normalized_vibe_contract.cache_clear()
    schema_definition._reference_contract.cache_clear()
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
        if hasattr(module, 'retire_preparation_capture'):
            monkeypatch.setattr(module, 'retire_preparation_capture', forbidden)
    monkeypatch.setattr(registry, 'MIGRATIONS', tuple(
        Migration(m.version, m.name, forbidden) for m in registry.MIGRATIONS))
    monkeypatch.setattr(regular, 'CHANGES', tuple(
        regular.Change(c.version, c.name, forbidden, c.validate) for c in regular.CHANGES))


def test_fresh_and_current_do_not_call_historical_bootstrap_or_validation(monkeypatch, tmp_path):
    from yt_playlist.core import schema_definition

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
        store.init_schema(defer_metadata=True)
        store.settings.set_setting('cold-current', 'persisted é')
        assert store.settings.get_setting('cold-current') == 'persisted é'
        before = list(store.conn.iterdump())
        store.init_schema(defer_metadata=True)
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
        migrate(conn)
        assert dict(conn.execute('SELECT key,value FROM settings')) == current.SEED_SETTINGS
        assert tuple(conn.execute('SELECT * FROM rec_preparation').fetchone()) == (1, 0, 0)
        assert tuple(conn.execute('SELECT * FROM rec_group_clock').fetchone()) == (1, 0)
        assert tuple(conn.execute('SELECT position,group_key,revision,cursor FROM rec_vibe_pending').fetchone()) == (
            1, '*', 2, '')
        assert conn.execute("SELECT 1 FROM genre_catalog WHERE name='Techno'").fetchone()
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE type='index' AND name='ix_genre_catalog_name'").fetchone()
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE type='view' AND name='listening_days'").fetchone()
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE type='trigger' AND name='playlists_scope_insert'").fetchone()
        from yt_playlist.core.db_rebuild import destination
        from yt_playlist.core import vibe_versions
        assert vibe_versions.normalized(conn)
        with closing(connect()) as candidate:
            candidate.execute('BEGIN IMMEDIATE')
            destination.initialize(candidate)
            cursor = None
            while True:
                batch = destination.indexes(candidate, candidate, cursor, 128)
                if batch.done:
                    break
                cursor = batch.cursor
            assert destination.finish(candidate, candidate, None, 10000).done
            candidate_objects = objects(candidate)
            # Only the candidate's explicit retained-source provenance is
            # additional. Every application table, index, view and guard is
            # compared in its actual stored format, without normalization.
            for name in ('database_rebuild_source_history', 'database_rebuild_source_changes'):
                assert candidate_objects.pop(name)[0] == 'table'
                assert not candidate.execute('SELECT 1 FROM '+name).fetchall()
            assert candidate_objects == objects(conn)
            for table, (kind, _) in candidate_objects.items():
                if kind != 'table':
                    continue
                columns = 'version,name' if table in ('schema_migrations', 'schema_changes') else '*'
                assert sorted(map(tuple, candidate.execute('SELECT '+columns+' FROM "'+table+'"')), key=repr) == sorted(
                    map(tuple, conn.execute('SELECT '+columns+' FROM "'+table+'"')), key=repr), table
            assert not candidate.execute('PRAGMA foreign_key_check').fetchall()
        conn.execute("INSERT INTO accounts(id,label,credential_ref,is_master) VALUES (1,'Main','test',1)")
        with pytest.raises(sqlite3.IntegrityError, match='CHECK constraint failed'):
            conn.execute("INSERT INTO listening_calendars(account_id,timezone,confirmed) VALUES (1,'UTC',2)")
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


@pytest.mark.parametrize('version', PREDECESSOR_VERSIONS)
def test_populated_predecessor_admission_preserves_exact_contents(version, predecessor_templates, monkeypatch):
    from yt_playlist.core.migrations import runner

    with closing(connect()) as conn, closing(sqlite3.connect(
            predecessor_templates[version].resolve().as_uri()+'?mode=ro',uri=True)) as predecessor:
        predecessor.backup(conn)
        populate(conn, version)
        before = list(conn.iterdump())
        changes = conn.total_changes
        admitted = []
        validate = runner._validate_migration_structure
        def stop_after_admission(connection, installed):
            assert connection is conn and installed == version
            validate(connection, installed)
            admitted.append(installed)
            raise RuntimeError(f'Completed installed entry validation {version}')
        monkeypatch.setattr(runner, '_validate_migration_structure', stop_after_admission)
        # Only the full frozen registry takes this entry-validation branch.
        # Interrupt after the actual validator, before any suffix is applied.
        with pytest.raises(MigrationError, match=f'Completed installed entry validation {version}'):
            migrate(conn, registry=registry.MIGRATIONS)
        assert admitted == [version]
        assert not conn.in_transaction, version
        assert conn.total_changes == changes, version
        assert list(conn.iterdump()) == before, version
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', version
        assert not conn.execute('PRAGMA foreign_key_check').fetchall(), version


def test_populated_historical_chain_preserves_each_boundary(
        tmp_path, predecessor_templates, numbered_contract, record_property):
    from yt_playlist.core.migrations import runner
    from yt_playlist.core.store import Store

    store = Store(tmp_path / 'populated-chain.db')
    try:
        conn = store.conn
        with closing(sqlite3.connect(predecessor_templates[1].resolve().as_uri()+'?mode=ro',uri=True)) as first:
            first.backup(conn)
        populate(conn, 1)
        steps = []
        for version in (*PREDECESSOR_VERSIONS, LATEST_VERSION):
            historical = list(conn.execute('SELECT * FROM schema_migrations ORDER BY version'))
            started = time.perf_counter()
            # Each real callback runs once as this single populated chain grows.
            migrate(conn, registry=registry.MIGRATIONS[:version])
            steps.append({'version': version, 'seconds': time.perf_counter() - started})
            assert runner.history(conn, registry.MIGRATIONS[:version]) == version
            assert list(conn.execute('SELECT * FROM schema_migrations WHERE version<=? ORDER BY version',
                                     (len(historical),))) == historical, version
            if version == LATEST_VERSION:
                expected = numbered_contract
            else:
                with closing(sqlite3.connect(
                        predecessor_templates[version].resolve().as_uri()+'?mode=ro',uri=True)) as predecessor:
                    expected = objects(predecessor)
            assert objects(conn) == expected, version
            assert tuple(conn.execute('SELECT id,title,artist FROM tracks').fetchone()) == (17, 'Song', 'Artist'), version
            assert conn.execute("SELECT value FROM settings WHERE key='personal-setting'").fetchone()[0] == 'retain', version
            if version < 33:
                assert tuple(conn.execute('SELECT snapshot_id,identity_key FROM history_items').fetchone()) == (
                    1, 'song|artist'), version
            else:
                assert conn.execute('SELECT count(*) FROM listening_observations').fetchone()[0] == 1, version
            assert not conn.execute('PRAGMA foreign_key_check').fetchall(), version
        before = list(conn.iterdump())
        migrate(conn, registry=registry.MIGRATIONS)
        assert list(conn.iterdump()) == before
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        record_property('populated_historical_steps', json.dumps(steps))
    finally:
        store.conn.close()


@pytest.mark.parametrize('version', FULL_REPLAY_VERSIONS)
def test_explicit_predecessor_replay_matches_current_contract(
        tmp_path, version, predecessor_templates, numbered_contract, monkeypatch, record_property):
    from tests.prepared_fixture_support import cleanup_memory_files, roots
    from yt_playlist.core import schema_definition
    from yt_playlist.core.migrations import runner
    from yt_playlist.core.store import Store

    # Every starting version still executes its entire real upgrade suffix.
    # Keep disk transactions at the start, middle and end of the history;
    # the remaining cases exercise the same SQL and retained rows in memory.
    path = tmp_path / 'upgrade.db' if version in (1, 24, 72) else ':memory:'
    store = Store(path)
    try:
        conn = store.conn
        source = predecessor_templates[version].resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(source, uri=True)) as predecessor:
            predecessor.backup(conn)
        populate(conn, version)
        inspections, validations, measurements = Counter(), [], []
        inspect = schema_definition._inspect_table
        validate = runner._validate_migration_structure

        def inspect_table(connection, name, sql):
            if connection is conn:
                inspections[(name, sql)] += 1
            return inspect(connection, name, sql)

        def validate_structure(connection, installed):
            if connection is conn:
                validations.append(installed)
            return validate(connection, installed)

        monkeypatch.setattr(schema_definition, '_inspect_table', inspect_table)
        monkeypatch.setattr(runner, '_validate_migration_structure', validate_structure)

        def measured_replay(phase):
            inspections.clear()
            validations.clear()
            before = schema_definition._reference_contract.cache_info()
            started = time.perf_counter()
            migrate(conn, registry=registry.MIGRATIONS)
            elapsed = time.perf_counter() - started
            after = schema_definition._reference_contract.cache_info()
            measurements.append({
                'phase': phase, 'seconds': elapsed, 'validation_versions': list(validations),
                'table_inspections': sum(inspections.values()),
                'distinct_table_definitions': len(inspections),
                'reference_constructions': after.misses - before.misses,
                'reference_reuses': after.hits - before.hits,
            })
            # Settings can acquire a new definition during older replays, but
            # each unchanged definition needs only one real owner inspection.
            settings = [count for (name, _sql), count in inspections.items() if name == 'settings']
            assert settings and max(settings) == 1

        # Exercise every genuine starting-version replay. Regular admission
        # consumes the resulting current layout and has one populated control
        # below, rather than repeating all its DDL after every starting version.
        measured_replay('upgrade')
        current.validate(conn)
        assert objects(conn) == numbered_contract
        for key in current.SEED_SETTINGS:
            assert conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone() is not None
        assert conn.execute("SELECT value FROM settings WHERE key='personal-setting'").fetchone()[0] == 'retain'
        assert tuple(conn.execute('SELECT id,title,artist FROM tracks').fetchone()) == (17, 'Song', 'Artist')
        assert conn.execute('SELECT COUNT(*) FROM listening_observations').fetchone()[0] == 1
        before = list(conn.iterdump())
        measured_replay('idempotence')
        assert sum(count for (name, _sql), count in inspections.items() if name == 'settings') == 1
        record_property('historical_replay', json.dumps(measurements))
        assert list(conn.iterdump()) == before
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        cleanup_memory_files(store, roots(store.conn))
        store.conn.close()
    assert not (tmp_path / 'backups').exists()


@pytest.mark.parametrize('change,error', [
    ('row', None),
    ('owner', 'identities'),
    ('index', 'ix_entries_recording'),
    ('seed', 'catalog identity'),
    ('history', 'contiguous prefix'),
    ('advance', None),
    ('advance_damage', 'identities'),
])
def test_historical_replay_rechecks_foreign_key_reservation_gap(
        tmp_path, predecessor_templates, monkeypatch, change, error):
    from yt_playlist.core import schema_definition
    from yt_playlist.core.migrations import runner

    class RebuildConnection(sqlite3.Connection):
        before_rebuild = None

        def execute(self, sql, *args, **kwargs):
            if (sql == 'BEGIN IMMEDIATE' and self.before_rebuild is not None
                    and not self.execute('PRAGMA foreign_keys').fetchone()[0]):
                callback, self.before_rebuild = self.before_rebuild, None
                assert not self.in_transaction
                callback()
            return super().execute(sql, *args, **kwargs)

    path = tmp_path / 'reservation.db'
    with closing(sqlite3.connect(path, factory=RebuildConnection)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        source = predecessor_templates[93].resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(source, uri=True)) as predecessor:
            predecessor.backup(conn)
        changed, validations, inspections = [], [], []
        validate, inspect = runner._validate_migration_structure, schema_definition._inspect_table

        def validate_structure(connection, version):
            if connection is conn:
                validations.append(version)
            return validate(connection, version)

        def inspect_table(connection, name, sql):
            if connection is conn:
                inspections.append(name)
            return inspect(connection, name, sql)

        monkeypatch.setattr(runner, '_validate_migration_structure', validate_structure)
        monkeypatch.setattr(schema_definition, '_inspect_table', inspect_table)

        def concurrent_commit():
            # Execute a real independent writer in the released reservation.
            # Advancing uses the actual v94 conversion, followed by optional
            # damage: the next entry check must precede v95's first write.
            with closing(connect(path)) as other:
                if change.startswith('advance'):
                    migrate(other, registry=registry.MIGRATIONS[:94])
                with other:
                    if change in ('owner', 'advance_damage'):
                        other.execute('ALTER TABLE identities RENAME COLUMN label TO damaged')
                    elif change == 'index':
                        other.execute('DROP INDEX ix_entries_recording')
                    elif change == 'seed':
                        other.execute("UPDATE settings SET value='changed' WHERE key='catalog_identity_version'")
                    elif change == 'history':
                        other.execute("UPDATE schema_migrations SET name='changed' WHERE version=93")
                    else:
                        other.execute("INSERT INTO settings VALUES ('concurrent-setting','committed')")
                changed.extend(other.iterdump())

        conn.before_rebuild = concurrent_commit
        if error:
            with pytest.raises(MigrationError, match=error):
                migrate(conn, registry=registry.MIGRATIONS)
            assert list(conn.iterdump()) == changed
            assert conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == (
                94 if change.startswith('advance') else 93)
        else:
            migrate(conn, registry=registry.MIGRATIONS)
            assert validations[:2] == [93, 94 if change == 'advance' else 93]
            assert inspections.count('identities') == 2
            assert conn.execute("SELECT value FROM settings WHERE key='concurrent-setting'").fetchone()[0] == 'committed'
            assert conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == LATEST_VERSION
            assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert conn.before_rebuild is None
        assert changed
        assert not conn.in_transaction
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1


def test_populated_historical_replay_admits_regular_schema_without_losing_rows(
        tmp_path, predecessor_templates, current_contract):
    from tests.regular_schema_support import admit_regular_schema
    from yt_playlist.core.store import Store

    store = Store(tmp_path/'admitted.db')
    try:
        source = predecessor_templates[1].resolve().as_uri()+'?mode=ro'
        with closing(sqlite3.connect(source, uri=True)) as predecessor:
            predecessor.backup(store.conn)
        populate(store.conn, 1)
        migrate(store.conn, registry=registry.MIGRATIONS)
        # Independent declarations verify every original row and program,
        # allowing only the named regular owners and their expected seed rows.
        admit_regular_schema(store)
        assert objects(store.conn) == current_contract
        before = list(store.conn.iterdump())
        migrate(store.conn)
        assert list(store.conn.iterdump()) == before
    finally:
        store.conn.close()


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
            assert objects(conn, canonical_versions=True) == objects(fresh, canonical_versions=True)
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


@pytest.mark.parametrize('start_version', [None, 107])
def test_initialized_state_matches_full_historical_chain(start_version):
    with closing(connect()) as upgraded, closing(connect()) as fresh:
        history = []
        if start_version is not None:
            migrate(upgraded, registry=registry.MIGRATIONS[:start_version])
            history = list(map(tuple, upgraded.execute('SELECT * FROM schema_migrations ORDER BY version')))
        migrate(upgraded, registry=registry.MIGRATIONS)
        assert list(map(tuple, upgraded.execute('SELECT * FROM schema_migrations ORDER BY version')))[:len(history)] == history
        migrate(upgraded)
        migrate(fresh)
        assert objects(upgraded, canonical_versions=True) == objects(fresh, canonical_versions=True)
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
            assert objects(conn, canonical_versions=True) == objects(fresh, canonical_versions=True)


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
    'DROP INDEX ix_album_feedback_response_pending',
    "DELETE FROM settings WHERE key='catalog_identity_version'",
    "UPDATE settings SET value='99' WHERE key='catalog_identity_version'",
    "UPDATE schema_migrations SET name='wrong' WHERE version=31",
    "DELETE FROM schema_migrations WHERE version=15",
    f"INSERT INTO schema_migrations VALUES ({LATEST_VERSION + 1},'future',1)",
    f"INSERT INTO schema_changes VALUES ({len(regular.CHANGES) + 1},'future',1)",
    'ALTER TABLE listening_calendars DROP COLUMN confirmed',
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


@pytest.mark.parametrize('owner', [current, regular], ids=['current', 'regular'])
def test_bootstrap_failure_rolls_back_all_objects_and_history(monkeypatch, owner):
    validate = owner.validate
    monkeypatch.setattr(owner, 'validate', lambda conn: (_ for _ in ()).throw(ValueError('injected bootstrap failure')))
    with connect() as conn:
        with pytest.raises(MigrationError, match='injected bootstrap failure'):
            migrate(conn)
        assert list(conn.execute('SELECT * FROM sqlite_master')) == []
        monkeypatch.setattr(owner, 'validate', validate)
        migrate(conn)
        current.validate(conn)
        assert regular.validate(conn) == len(regular.CHANGES)
        assert not conn.in_transaction


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
