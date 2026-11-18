"""Independent consumer DDL must retain completed and pending production changes."""
from contextlib import closing
import sqlite3

import pytest

from tests.regular_schema_support import register_independent_ddl
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import install_numbered_input, install_regular_input
from yt_playlist.core.migrations import regular


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These real SQL worker checks construct no HTTP application."""


@pytest.mark.parametrize('completed', [False, True])
def test_pending_consumer_ddl_preserves_the_completed_prefix_and_validates_live_sql(monkeypatch, completed):
    bound_catalog(monkeypatch, entries=(('jazz', 'Jazz'),))
    with closing(sqlite3.connect(':memory:')) as conn:
        production = tuple(regular.CHANGES)
        prefix = len(production) if completed else 37
        install_numbered_input(conn, 107, regular_owners=production[:prefix])
        history = list(map(tuple, conn.execute('SELECT * FROM schema_changes ORDER BY version')))
        numbered = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        assert len(history) == prefix
        change = register_independent_ddl(monkeypatch, conn)
        assert list(map(tuple, conn.execute('SELECT * FROM schema_changes ORDER BY version'))) == history
        regular.migrate(conn, clock=lambda: 123.)
        expected = [*history, *((entry.version, entry.name, 123.) for entry in production[prefix:])]
        assert list(map(tuple, conn.execute('SELECT * FROM schema_changes ORDER BY version'))) == [
            *expected, (len(production) + 1, 'independent_preparation_ddl', 123.)]
        assert regular.validate(conn) == change.version
        assert conn.execute('SELECT * FROM independent_preparation_ddl').fetchall() == []
        assert list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == numbered
        with conn:
            conn.execute('DROP TABLE independent_preparation_ddl')
        with pytest.raises(sqlite3.OperationalError, match='independent_preparation_ddl'):
            regular.validate(conn)
