"""Track collection ownership changes across real database connections."""
from contextlib import closing
import sqlite3

import pytest

from tests.store_fixture_support import without_query_timing
from yt_playlist.core import collection_recovery_epoch as epoch
from yt_playlist.core.migrations import MigrationError, migrate


pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """SQL recovery does not create the web application."""


def test_current_schema_has_collection_ownership_guards(file_backed_store):
    epoch.validate(file_backed_store.conn)
    epoch.validate_keys(file_backed_store.conn)

def test_epoch_tracks_raw_external_writes_and_rollback(file_backed_store):
    store = file_backed_store
    assert store.sync.repository.state_facts()['attempt'] is None
    store.playlist_recipes.set_recipe('P', {'kept': True}, 1)
    filename = store.conn.execute("SELECT recipe_recovery_filename FROM playlist_scopes WHERE id='P'").fetchone()[0]
    revision = lambda: epoch.revision(store.conn)
    original = revision()
    with closing(sqlite3.connect(store._db_path)) as writer:
        writer.execute("UPDATE playlist_scopes SET recipe_recovery_filename=? WHERE id='P'", (filename + '.new',))
        assert revision() == original  # uncommitted ownership is not current
        writer.rollback()
        assert revision() == original
        with writer:
            writer.execute("UPDATE playlist_scopes SET recipe_recovery_filename=? WHERE id='P'", (filename,))
            writer.execute("INSERT INTO settings VALUES ('independent-write','kept')")
        assert revision() == original
        with writer:
            writer.execute("UPDATE playlist_scopes SET recipe_recovery_filename=? WHERE id='P'", (filename + '.new',))
        assert revision() == original + 1
        with writer:
            writer.execute("DELETE FROM playlist_scopes WHERE id='P'")
        assert revision() == original + 2


def test_restart_rejects_missing_ownership_trigger(file_backed_store):
    store = file_backed_store
    with store.conn:
        store.conn.execute('DROP TRIGGER collection_recovery_changed_playlist_recipe_update')
    with pytest.raises(MigrationError, match='collection_recovery_changed_playlist_recipe_update'):
        migrate(store.conn)
