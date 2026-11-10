"""Previously installed development schemas remain readable and upgrade intact."""
from contextlib import closing
import sqlite3
import hashlib
import threading

import pytest

from tests.genre_catalog_support import bound_catalog
from tests.home_fixture_support import install_home_routes
from tests.store_fixture_support import reuse_reporting_reader
from yt_playlist.core.migrations import MigrationError, current, migrate, registry, runner
from yt_playlist.core.migrations import v092_installed_genre_contract as repair


def earlier_database(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('BEGIN IMMEDIATE')
    # Install the exact known earlier contract rather than relabelling a fresh
    # latest schema. Admission is checked separately against literal old DDL.
    for sql in repair.legacy_statements(current.statements(version=88)):
        conn.execute(sql)
    from yt_playlist.core import genre_identity
    genre_identity.seed_catalog(conn)
    conn.executemany('INSERT INTO settings VALUES (?,?)', current.SEED_SETTINGS.items())
    conn.executemany('INSERT INTO schema_migrations VALUES (?,?,?)',
                     [(m.version, m.name, 1) for m in registry.MIGRATIONS[:88]])
    conn.execute("INSERT INTO taste_axes(id,kind,genre_key) VALUES (7,'genre','jazz')")
    conn.execute("INSERT INTO taste_axis_legacy VALUES ('rec_lean','genre:Jazz','genre:Jazz',7)")
    conn.execute('INSERT INTO rec_lean(axis_id,value,updated_at) VALUES (7,1.4,10)')
    conn.commit()
    return conn


@pytest.fixture(scope='module')
def earlier_template():
    with pytest.MonkeyPatch.context() as patch:
        bound_catalog(patch, entries=(('jazz', 'Jazz'),))
        source = earlier_database(':memory:')
    try:
        source.execute('PRAGMA query_only=ON')
        yield source
    finally:
        source.close()


@pytest.fixture
def earlier_install(earlier_template):
    def restore(path):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        earlier_template.backup(conn)
        return conn
    return restore


def test_v88_genre_input_keeps_exact_legacy_lean_and_numbered_history(earlier_template):
    assert earlier_template.execute('PRAGMA query_only').fetchone()[0] == 1
    assert earlier_template.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 88
    assert tuple(earlier_template.execute('SELECT axis_id,value,updated_at FROM rec_lean').fetchone()) == (7, 1.4, 10)
    assert not earlier_template.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('version', range(86, 92))
def test_known_legacy_alternatives_match_the_complete_sqlite_contract(version):
    # Independently execute the complete old layout. Reference construction
    # may omit unchanged owners only when every accepted alternative still
    # has exactly the observations produced by that full SQLite database.
    statements = tuple(current.statements(version=version))
    complete = current._schema_contract(repair.legacy_statements(statements))
    reference, alternatives = repair._contracts(version)
    assert reference == current._schema_contract(statements)
    names = set((*repair.REPLACEMENTS, 'taste_axes', 'taste_axis_legacy')) & complete.keys()
    assert alternatives == {name: complete[name] for name in names}


def test_unknown_trigger_damage_is_rejected_without_repair(tmp_path, earlier_install):
    conn = earlier_install(tmp_path / 'damaged.db')
    try:
        # A previously admitted connection must still reject later damage;
        # cached reference definitions never cache live admission results.
        migrate(conn, defer_validation=True)
        with conn:
            conn.execute('DROP TRIGGER catalog_tracks_update')
            conn.execute('CREATE TRIGGER catalog_tracks_update INSTEAD OF UPDATE ON catalog_tracks '
                         "BEGIN SELECT RAISE(ABORT,'unknown change'); END")
        before = conn.total_changes
        with pytest.raises(MigrationError, match='Incompatible current trigger: catalog_tracks_update'):
            migrate(conn, defer_validation=True)
        assert conn.total_changes == before
        assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 88
    finally:
        conn.close()


def test_constraint_upgrade_failure_rolls_back_without_discarding_legacy_evidence(tmp_path, earlier_install):
    conn = earlier_install(tmp_path / 'invalid.db')
    try:
        with conn:
            conn.execute("INSERT INTO taste_axes(id,kind) VALUES (8,'other')")
        with pytest.raises(MigrationError, match='migration 92 .*CHECK constraint failed'):
            migrate(conn, registry=registry.MIGRATIONS)
        assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 91
        assert conn.execute('SELECT kind,value FROM taste_axes WHERE id=8').fetchone()[:] == ('other',None)
        assert len(conn.execute('PRAGMA table_info(taste_axis_legacy)').fetchall()) == 4
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='upgraded_taste_axes'").fetchone()
    finally:
        conn.close()


def test_exact_native_v88_triggers_admit_writable_app_without_bulk_conversion(
        tmp_path, earlier_template, monkeypatch, caplog):
    """Keep admission, HTTP, all regular DDL and stale reads in one workflow."""
    from yt_playlist.core.store import Store
    from yt_playlist.core.db_diagnostics import DiagnosticConnection
    from yt_playlist.core.migrations import regular
    from yt_playlist.providers import genres

    bound_catalog(monkeypatch, entries=(('jazz', 'Jazz'),))
    monkeypatch.setattr(genres, 'configure', lambda *args, **kwargs: None)
    path = tmp_path / 'native-v88.db'
    conn = sqlite3.connect(path)
    earlier_template.backup(conn)
    hashes = {'catalog_tracks_update': '223ba10042f4c50345b0568ed9d55ca99a44ce988ef29c7457ddc2dd8c7956bb',
              'tracks_update': 'ecca94598461eede0a44fb1a0e389ed90cdff2a58074ecd42544b86f2de97021'}
    try:
        for name, digest in hashes.items():
            sql = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()[0]
            assert hashlib.sha256(sql.encode()).hexdigest() == digest
        history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    finally:
        conn.close()
    store = Store(path)
    maintenance = None
    client = None
    release = threading.Event()
    try:
        store._bind_repositories()
        store.tracks.upsert_track('preserved', 'Preserved song', 'Artist', None, 213)
        store.settings.set_setting('upgrade-preserved', 'original')
        store.rec_ledger.record_serving_policy('preserved-policy', {'algorithm': 'saved'}, 1)
        catalog = list(map(tuple, store.conn.execute('SELECT * FROM recordings ORDER BY id')))
        evidence = store.conn.execute('SELECT count(*) FROM metadata_observations').fetchone()[0]
        store.init_schema(defer_metadata=True)
        assert not store.activity_paused and store._schema_compatibility
        store.settings.set_setting('during-schema-update', 'writable')
        assert list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        from fastapi.testclient import TestClient
        from yt_playlist.web.app import create_app
        with pytest.MonkeyPatch.context() as patch:
            install_home_routes(patch)
            app = create_app(store, lambda: {}, background_work=False)
        client = TestClient(app, base_url='http://127.0.0.1',backend_options={'use_uvloop':True})
        with pytest.MonkeyPatch.context() as patch, reuse_reporting_reader(store, patch):
            assert client.get('/').status_code == 200
        maintenance = app.state.ctx.storage_maintenance
        # Exercise a foreground write between two actual committed changes.
        reached = threading.Event()
        middle = len(regular.CHANGES) // 2
        commit = DiagnosticConnection.commit

        def gated_commit(conn):
            result = commit(conn)
            if threading.current_thread() is maintenance._thread and not reached.is_set():
                applied = conn.execute('SELECT count(*) FROM schema_changes').fetchone()[0]
                if applied == middle:
                    assert not conn.in_transaction
                    reached.set()
                    assert release.wait(15), 'Regular worker was not released'
            return result

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(DiagnosticConnection, 'commit', gated_commit)
            maintenance.start()
            assert reached.wait(15), maintenance.phase
            assert 0 < store.conn.execute('SELECT count(*) FROM schema_changes').fetchone()[0] < len(regular.CHANGES)
            assert maintenance._thread.is_alive()
            store.settings.set_setting('during-regular-ddl', 'writable')
            assert not store.activity_paused
            release.set()
            assert maintenance.ready.wait(15), maintenance.phase
            maintenance.join(1)
        assert maintenance.phase == 'done'
        assert not store._schema_recovery_pending and not store.activity_paused
        assert not any(record.levelname in ('WARNING', 'ERROR') and record.name ==
                       'yt_playlist.core.storage_maintenance' for record in caplog.records)
        assert list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        assert list(map(tuple,store.conn.execute('SELECT version,name FROM schema_changes ORDER BY version'))) == [
            (change.version,change.name) for change in regular.CHANGES]
        assert list(map(tuple, store.conn.execute('SELECT * FROM recordings ORDER BY id'))) == catalog
        assert store.conn.execute('SELECT count(*) FROM metadata_observations').fetchone()[0] == evidence
        assert store.settings.get_setting('upgrade-preserved') == 'original'
        assert store.rec_ledger.policy_configuration('preserved-policy') == {'algorithm': 'saved'}
        assert tuple(store.conn.execute('SELECT axis_id,value,updated_at FROM rec_lean').fetchone()) == (7,1.4,10)
        with store.read_snapshot() as snapshot:
            assert snapshot.store.settings.get_setting('after-upgrade') is None
            snapshot.release_reader()
        store.settings.set_setting('after-upgrade', 'writable')
        assert not snapshot.publish(lambda: pytest.fail('Rebound revision guards accepted stale inputs'))
        with pytest.MonkeyPatch.context() as patch, reuse_reporting_reader(store, patch):
            assert client.get('/').status_code == 200
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        release.set()
        if maintenance is not None:
            maintenance.stop()
            maintenance.join(5)
        if client is not None:
            client.close()
        store.conn.close()
        genres.set_active(None)


def test_bounded_repair_of_already_upgraded_missing_guards_preserves_applied_history(
        tmp_path, monkeypatch):
    from yt_playlist.core.migrations import regular
    from yt_playlist.core.preparation import installed_genre_contract

    conn = sqlite3.connect(tmp_path / 'previously-upgraded.db')
    conn.row_factory = sqlite3.Row
    try:
        bound_catalog(monkeypatch)
        migrate(conn, registry=registry.MIGRATIONS[:79])
        regular.migrate(conn, changes=regular.CHANGES[:7])
        with monkeypatch.context() as former:
            former.setattr(regular, 'after_owner_upgrade', lambda conn: None)
            migrate(conn, registry=registry.MIGRATIONS)
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name LIKE 'installed_taste_axis_guard_%'").fetchone()
        history = conn.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall()
        changes = conn.execute('SELECT * FROM schema_changes ORDER BY version').fetchall()
        install = installed_genre_contract.install

        def interrupted(conn, context):
            install(conn, context)
            raise RuntimeError('interrupted guard repair')

        with monkeypatch.context() as held:
            held.setattr(installed_genre_contract, 'install', interrupted)
            with pytest.raises(RuntimeError, match='interrupted guard repair'):
                regular.migrate(conn)
        assert conn.execute('SELECT * FROM schema_changes ORDER BY version').fetchall() == changes
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name LIKE 'installed_taste_axis_guard_%'").fetchone()
        regular.migrate(conn)
        regular.validate(conn)
        assert conn.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall() == history
        assert conn.execute('SELECT * FROM schema_changes ORDER BY version').fetchall()[:7] == changes
        assert tuple(conn.execute('SELECT version,name FROM schema_changes WHERE version=8').fetchone()) == (8, 'installed_taste_guard_lifecycle')
    finally:
        conn.close()
