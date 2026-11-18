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


@pytest.mark.parametrize('version', [107])
def test_completed_epoch_admission_validates_all_regular_owners(tmp_path, epoch_input, version):
    with closing(sqlite3.connect(tmp_path / 'completed-admission.db')) as conn:
        conn.row_factory = sqlite3.Row
        epoch_input(conn, version, len(regular.CHANGES))
        before = list(map(tuple, conn.execute('SELECT * FROM schema_changes ORDER BY version')))
        history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        regular.validate(conn)
        epoch.validate(conn)
        epoch.validate_keys(conn)
        assert list(map(tuple, conn.execute('SELECT * FROM schema_changes ORDER BY version'))) == before
        assert list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history




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
