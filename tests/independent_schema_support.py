"""A real independent DDL owner for preparation failure and concurrency tests."""
import pytest

from yt_playlist.core.migrations import regular


@pytest.fixture
def independent_regular_ddl(monkeypatch):
    # These cases own independence from preparation, rather than replay of the
    # application's entire schema chain. test_independent_schema_changes owns
    # the real installed registry and its upgrade ordering.
    def install(conn, context):
        conn.execute('CREATE TABLE independent_preparation_ddl(value TEXT)')

    def validate(conn):
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' "
                            "AND name='independent_preparation_ddl'").fetchone()

    monkeypatch.setattr(regular, 'CHANGES', (
        regular.Change(1, 'independent_preparation_ddl', install, validate),))
