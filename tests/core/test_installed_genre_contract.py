"""Previously installed development schemas remain readable and upgrade intact."""
import sqlite3
import hashlib

import pytest

from yt_playlist.core.migrations import MigrationError, current, migrate, registry, runner
from yt_playlist.core.migrations import v092_installed_genre_contract as repair


def earlier_database(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
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


def test_earlier_install_is_admitted_and_numbered_upgrade_preserves_references(tmp_path):
    path = tmp_path / 'earlier.db'
    conn = earlier_database(path)
    try:
        # These independent assertions pin the definitions observed in the
        # user's v88 install, including its four-column archive.
        assert [r[1] for r in conn.execute('PRAGMA table_info(taste_axis_legacy)')] == [
            'table_name', 'row_key', 'raw_axis', 'axis_id']
        choice = conn.execute("SELECT sql FROM sqlite_schema WHERE name='entry_field_choices'").fetchone()[0]
        assert 'WHERE key=o.genre_key' in choice
        assert 'genre_redirects' not in choice
        trigger = conn.execute("SELECT sql FROM sqlite_schema WHERE name='catalog_tracks_update'").fetchone()[0]
        assert 'SELECT key FROM genre_catalog WHERE key=NEW.genre OR name=NEW.genre COLLATE NOCASE' in trigger
        before = conn.total_changes
        assert migrate(conn, defer_validation=True) is True
        assert conn.total_changes == before
        assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 88
        migrate(conn, registry=registry.MIGRATIONS)
        current.validate(conn)
        assert tuple(conn.execute('SELECT axis_id,value,updated_at FROM rec_lean').fetchone()) == (7,1.4,10)
        archive = dict(conn.execute('SELECT * FROM taste_axis_legacy').fetchone())
        assert {k: archive[k] for k in ('table_name','row_key','raw_axis','axis_id')} == {
            'table_name':'rec_lean', 'row_key':'genre:Jazz', 'raw_axis':'genre:Jazz', 'axis_id':7}
        assert archive['lean_value'] is None  # Never invent uncaptured historical values.
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute("INSERT INTO taste_axes(kind) VALUES ('other')")
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        # Adopt the independent ledger before checking routine idempotency.
        migrate(conn)
        before = conn.total_changes
        migrate(conn)
        assert conn.total_changes == before
    finally:
        conn.close()


def test_unknown_trigger_damage_is_rejected_without_repair(tmp_path):
    conn = earlier_database(tmp_path / 'damaged.db')
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


def test_constraint_upgrade_failure_rolls_back_without_discarding_legacy_evidence(tmp_path):
    conn = earlier_database(tmp_path / 'invalid.db')
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


def test_exact_native_v88_triggers_admit_writable_app_without_bulk_conversion(tmp_path, caplog):
    """Pin the actual read-only native capture, then exercise an isolated Store."""
    from fastapi.testclient import TestClient
    from yt_playlist.core.store import Store
    from yt_playlist.core.migrations import regular
    from yt_playlist.providers import genres
    from yt_playlist.web.app import create_app

    path = tmp_path / 'native-v88.db'
    conn = earlier_database(path)
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
        app = create_app(store, lambda: {}, background_work=False)
        client = TestClient(app, base_url='http://127.0.0.1',backend_options={'use_uvloop':True})
        assert client.get('/').status_code == 200
        maintenance = app.state.ctx.storage_maintenance
        maintenance.start()
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
        assert client.get('/').status_code == 200
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        if maintenance is not None:
            maintenance.stop()
            maintenance.join(5)
        store.conn.close()
        genres.set_active(None)


@pytest.mark.parametrize('version', [79, 85, 87, 92])
def test_regular_guards_follow_later_explicit_owner_upgrades(tmp_path, version):
    from yt_playlist.core.migrations import regular
    from yt_playlist.core.preparation import installed_genre_contract
    from yt_playlist.core.store import Store

    path = tmp_path / 'owner-upgrade.db'
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        migrate(conn, registry=registry.MIGRATIONS[:version])
        with conn:
            conn.execute("INSERT INTO settings VALUES ('retained-guard-setting','original')")
            if version < 88:
                conn.execute("INSERT INTO rec_weights VALUES ('genre:Jazz',1.25,10)")
            else:
                conn.execute("INSERT INTO taste_axes(kind,genre_key) VALUES ('genre','jazz')")
                conn.execute("INSERT INTO rec_weights SELECT id,1.25,10 FROM taste_axes WHERE genre_key='jazz'")
        history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        regular.migrate(conn)
        changes = list(map(tuple, conn.execute('SELECT * FROM schema_changes ORDER BY version')))
        if version < 88:
            assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name LIKE 'installed_taste_axis_guard_%'").fetchone()
            migrate(conn, registry=registry.MIGRATIONS[:88])
            installed_genre_contract.validate(conn)
            with pytest.raises(sqlite3.IntegrityError, match='Invalid future taste axis'), conn:
                conn.execute("INSERT INTO taste_axes(kind) VALUES ('other')")
        migrate(conn, registry=registry.MIGRATIONS)
        regular.validate(conn)
        assert list(map(tuple, conn.execute('SELECT * FROM schema_changes ORDER BY version'))) == changes
        assert list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))[:version] == history
        assert conn.execute("SELECT value FROM settings WHERE key='retained-guard-setting'").fetchone()[0] == 'original'
        assert tuple(conn.execute('SELECT a.kind,a.genre_key,w.weight,w.updated_at FROM rec_weights w '
            'JOIN taste_axes a ON a.id=w.axis_id').fetchone()) == ('genre', 'jazz', 1.25, 10)
        assert {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'installed_taste_axis_guard_%'")} == {
            'installed_taste_axis_guard_insert', 'installed_taste_axis_guard_update'}
        with pytest.raises(sqlite3.IntegrityError), conn:
            conn.execute("UPDATE taste_axes SET kind='other',genre_key=NULL")
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        conn.close()
    reopened = Store(path)
    try:
        reopened.init_schema()
        assert reopened.settings.get_setting('retained-guard-setting') == 'original'
        reopened.settings.set_setting('continuing-guard-write', 'available')
        regular.validate(reopened.conn)
    finally:
        reopened.conn.close()


def test_bounded_repair_of_already_upgraded_missing_guards_preserves_applied_history(tmp_path, monkeypatch):
    from yt_playlist.core.migrations import regular
    from yt_playlist.core.preparation import installed_genre_contract

    conn = sqlite3.connect(tmp_path / 'previously-upgraded.db')
    conn.row_factory = sqlite3.Row
    try:
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
