"""Install a bounded cross-connection ownership fence on real installed layouts."""
from contextlib import closing, ExitStack
import sqlite3

import pytest

from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import install_numbered_input, native_sql_calls, without_query_timing
from yt_playlist.core import collection_recovery as recovery, collection_recovery_epoch as epoch, schema_inspection
from yt_playlist.core.migrations import MigrationError, migrate, registry, regular
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Epoch installation and SQL recovery do not create the web application."""


@pytest.fixture
def epoch_input(monkeypatch):
    bound_catalog(monkeypatch, entries=(('rock', 'Rock'),))

    with ExitStack() as inspections:
        def install(conn, version, applied):
            conn.execute('PRAGMA synchronous=OFF')
            # Completed lower regular owners do not need a separate admission
            # and commit at each historical step. The adoption runner is real.
            install_numbered_input(conn, version, regular_owners=regular.CHANGES[:applied])
            # Inspect unchanged CREATE TABLE definitions once per private
            # connection; actual DDL, validators and rollback remain live.
            inspections.enter_context(schema_inspection.reuse_tables(conn))

        yield install


@pytest.mark.parametrize('version', [79, 107])
@pytest.mark.parametrize('applied', [26, 27])
def test_adoption_does_not_read_owners_or_rewrite_history(
        tmp_path, epoch_input, version, applied):
    store = Store(tmp_path / 'installed.db')
    try:
        epoch_input(store.conn, version, applied)
        history = list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        changes = list(map(tuple, store.conn.execute('SELECT * FROM schema_changes ORDER BY version')))

        def no_owner_reads(action, table, column, database, source):
            if action == sqlite3.SQLITE_READ and database == 'main' and table in (*recovery.OWNERS, 'action_backups'):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        store.conn.set_authorizer(no_owner_reads)
        try:
            regular.migrate(store.conn)
        finally:
            store.conn.set_authorizer(None)
        assert list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        result = list(map(tuple, store.conn.execute('SELECT * FROM schema_changes ORDER BY version')))
        assert result[:applied] == changes
        assert [(version, name) for version, name, _ in result[applied:]] == [
            (change.version, change.name) for change in regular.CHANGES[applied:]]
        assert store.conn.execute(f'SELECT revision FROM {epoch.TABLE}').fetchone()[0] == 0
        if version == 79:
            migrate(store.conn, registry=registry.MIGRATIONS)
        epoch.validate(store.conn)
        epoch.validate_keys(store.conn)
        regular.validate(store.conn)
        with store.conn:
            store.conn.execute("INSERT INTO settings VALUES ('continued-write','kept')")
        assert store.conn.execute("SELECT value FROM settings WHERE key='continued-write'").fetchone()[0] == 'kept'
    finally:
        store.conn.close()


@pytest.mark.parametrize('version', [79, 107])
def test_completed_epoch_admission_validates_all_regular_owners(tmp_path, epoch_input, version):
    with closing(sqlite3.connect(tmp_path / 'completed-admission.db')) as conn:
        conn.row_factory = sqlite3.Row
        epoch_input(conn, version, 35)
        before = list(map(tuple, conn.execute('SELECT * FROM schema_changes ORDER BY version')))
        history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        regular.validate(conn)
        epoch.validate(conn)
        epoch.validate_keys(conn)
        assert list(map(tuple, conn.execute('SELECT * FROM schema_changes ORDER BY version'))) == before
        assert list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history


@pytest.mark.parametrize('source,target', [(93, 94), (94, 96),
    (96, 97), (97, 98), (100, 101)], ids=['collection', 'taste',
                                      'documents', 'actions', 'discovery'])
def test_explicit_owner_upgrade_extends_adopted_epoch_without_rewriting_receipts(
        tmp_path, epoch_input, source, target):
    with closing(sqlite3.connect(tmp_path / 'owner-upgrade.db')) as conn:
        conn.row_factory = sqlite3.Row
        epoch_input(conn, source, 28)
        before = list(map(tuple, conn.execute('SELECT * FROM schema_changes ORDER BY version')))
        guards = {name for name, in conn.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'collection_recovery_changed_%'")}
        migrate(conn, registry=registry.MIGRATIONS[:target])
        epoch.validate(conn)
        epoch.validate_keys(conn)
        after = {name for name, in conn.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'collection_recovery_changed_%'")}
        assert after > guards
        assert list(map(tuple, conn.execute('SELECT * FROM schema_changes ORDER BY version'))) == before
        assert conn.execute('SELECT revision FROM ' + epoch.TABLE).fetchone()[0] == 0
        assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == target
        regular.validate(conn)
        with conn:
            conn.execute("INSERT INTO settings VALUES ('continued-write','kept')")
        assert conn.execute("SELECT value FROM settings WHERE key='continued-write'").fetchone()[0] == 'kept'


@pytest.mark.parametrize('version', [79, 107])
@pytest.mark.parametrize('applied,target', [(28, 31), (31, 35)])
def test_later_regular_adoption_preserves_epoch_without_reading_owners(
        tmp_path, epoch_input, version, applied, target):
    with closing(sqlite3.connect(tmp_path / 'later-adoption.db')) as conn:
        conn.row_factory = sqlite3.Row
        epoch_input(conn, version, applied)
        history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        changes = list(map(tuple, conn.execute('SELECT * FROM schema_changes ORDER BY version')))
        revision = conn.execute('SELECT revision FROM ' + epoch.TABLE).fetchone()[0]

        def no_owner_reads(action, table, column, database, source):
            return sqlite3.SQLITE_DENY if (action == sqlite3.SQLITE_READ and database == 'main'
                and table in (*recovery.OWNERS, 'action_backups')) else sqlite3.SQLITE_OK

        conn.set_authorizer(no_owner_reads)
        try:
            regular.migrate(conn, changes=regular.CHANGES[:target])
        finally:
            conn.set_authorizer(None)
        assert list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        result = list(map(tuple, conn.execute('SELECT * FROM schema_changes ORDER BY version')))
        assert result[:applied] == changes
        assert [(version, name) for version, name, _ in result[applied:]] == [
            (change.version, change.name) for change in regular.CHANGES[applied:target]]
        assert conn.execute('SELECT revision FROM ' + epoch.TABLE).fetchone()[0] == revision
        epoch.validate(conn)
        epoch.validate_keys(conn)
        with conn:
            conn.execute("INSERT INTO settings VALUES ('continued-write','kept')")
        assert conn.execute("SELECT value FROM settings WHERE key='continued-write'").fetchone()[0] == 'kept'


@pytest.mark.parametrize('declarations,applied', [('definitions', 26), ('key_definitions', 27)])
def test_failed_install_rolls_back_ddl_and_receipt(tmp_path, epoch_input, monkeypatch,
                                                declarations, applied):
    with closing(sqlite3.connect(tmp_path / 'installed.db')) as conn:
        conn.row_factory = sqlite3.Row
        epoch_input(conn, 107, applied)
        before = list(conn.iterdump())
        definitions = getattr(epoch, declarations)

        def interrupted(connection):
            for index, definition in enumerate(definitions(connection)):
                yield definition
                if index == 1:
                    raise RuntimeError('Interrupted collection epoch installation')

        with monkeypatch.context() as fault:
            fault.setattr(epoch, declarations, interrupted)
            with pytest.raises(RuntimeError, match='Interrupted collection epoch'):
                regular.migrate(conn)
        assert list(conn.iterdump()) == before
        regular.migrate(conn)
        epoch.validate(conn)
        epoch.validate_keys(conn)


def test_epoch_tracks_raw_external_writes_and_rollback(file_backed_store):
    store = file_backed_store
    store.playlist_recipes.set_recipe('P', {'kept': True}, 1)
    filename = store.conn.execute('SELECT recovery_filename FROM rec_recipes').fetchone()[0]
    revision = lambda: store.conn.execute(f'SELECT revision FROM {epoch.TABLE}').fetchone()[0]
    original = revision()
    with closing(sqlite3.connect(store._db_path)) as writer:
        writer.execute('UPDATE rec_recipes SET recovery_filename=? WHERE id=1', (filename + '.new',))
        assert revision() == original  # uncommitted ownership is not current
        writer.rollback()
        assert revision() == original
        with writer:
            writer.execute('UPDATE rec_recipes SET recovery_filename=? WHERE id=1', (filename,))
            writer.execute("INSERT INTO settings VALUES ('independent-write','kept')")
        assert revision() == original
        with writer:
            writer.execute('UPDATE rec_recipes SET recovery_filename=? WHERE id=1', (filename + '.new',))
        assert revision() == original + 1
        with writer:
            writer.execute('DELETE FROM rec_recipes WHERE id=1')
        assert revision() == original + 2


def test_restart_rejects_missing_ownership_trigger(file_backed_store):
    store = file_backed_store
    with store.conn:
        store.conn.execute('DROP TRIGGER collection_recovery_changed_rec_recipes_update')
    with pytest.raises(MigrationError, match='collection recovery epoch'):
        regular.validate(store.conn)


def test_retained_copy_preserves_epoch_and_continuing_owner_writes(file_backed_store):
    from yt_playlist.core.db_rebuild import job

    store = file_backed_store
    store.playlist_recipes.set_recipe('P', {'before': 'copy'}, 1)
    original = store.conn.execute(f'SELECT revision FROM {epoch.TABLE}').fetchone()[0]
    report = job.run(store)
    assert report['phase'] == 'complete'
    assert report['copy']['tables'][epoch.TABLE]['rows'] == 1
    assert store.conn.execute(f'SELECT revision FROM {epoch.TABLE}').fetchone()[0] == original
    assert store.playlist_recipes.get_recipe('P') == {'before': 'copy'}
    store.playlist_recipes.set_recipe('P', {'after': 'copy'}, 2)
    assert store.conn.execute(f'SELECT revision FROM {epoch.TABLE}').fetchone()[0] == original + 1
    assert store.playlist_recipes.get_recipe('P') == {'after': 'copy'}
    epoch.validate(store.conn)
