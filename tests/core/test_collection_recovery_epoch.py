"""Install a bounded cross-connection ownership fence on real installed layouts."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.core import collection_recovery as recovery, collection_recovery_epoch as epoch
from yt_playlist.core.migrations import MigrationError, migrate, registry, regular
from yt_playlist.core.store import Store


@pytest.mark.parametrize('version', [79, 107])
@pytest.mark.parametrize('applied', [26, 27])
def test_adoption_does_not_read_owners_or_rewrite_history(
        tmp_path, _pristine_installed_schema, version, applied):
    store = Store(tmp_path / 'installed.db')
    try:
        _pristine_installed_schema(version).backup(store.conn)
        regular.migrate(store.conn, changes=regular.CHANGES[:applied])
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
        assert list(map(tuple, store.conn.execute('SELECT * FROM schema_changes ORDER BY version')))[:applied] == changes
        assert store.conn.execute(f'SELECT revision FROM {epoch.TABLE}').fetchone()[0] == 0
        # A subsequent explicit owner upgrade installs its newly available
        # filename guards without editing the already-applied regular receipt.
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


@pytest.mark.parametrize('declarations,applied', [('definitions', 26), ('key_definitions', 27)])
def test_failed_install_rolls_back_ddl_and_receipt(tmp_path, _pristine_installed_schema, monkeypatch,
                                                declarations, applied):
    with closing(sqlite3.connect(tmp_path / 'installed.db')) as conn:
        conn.row_factory = sqlite3.Row
        _pristine_installed_schema(107).backup(conn)
        regular.migrate(conn, changes=regular.CHANGES[:applied])
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
