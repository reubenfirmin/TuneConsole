"""Independent preparation admission against the unchanged historical installers."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.core import schema_admission
from yt_playlist.core.migrations import migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.schema_contract import MigrationError


@pytest.mark.parametrize('version', [79, 80, 81, 82, 83, 84, 85, 86, 88, 91, 92, 93, 99, 104, 107])
def test_preparation_admits_frozen_installed_schemas_without_writing(version):
    with closing(sqlite3.connect(':memory:')) as conn, conn:
        conn.row_factory = sqlite3.Row
        migrate(conn, registry=MIGRATIONS[:version])
        conn.execute('PRAGMA query_only=ON')
        changes = conn.total_changes
        assert schema_admission.history(conn) == version
        schema_admission.validate_version(conn, version, foreign_keys=False, data=False)
        assert conn.total_changes == changes


@pytest.mark.parametrize('damage', ['history_name', 'history_gap', 'future', 'column', 'trigger'])
def test_preparation_rejects_changed_history_and_owner_definitions(damage):
    with closing(sqlite3.connect(':memory:')) as conn, conn:
        conn.row_factory = sqlite3.Row
        migrate(conn, registry=MIGRATIONS[:79])
        if damage == 'history_name':
            conn.execute("UPDATE schema_migrations SET name='unrecognized' WHERE version=1")
        elif damage == 'history_gap':
            conn.execute('DELETE FROM schema_migrations WHERE version=1')
        elif damage == 'future':
            conn.execute('INSERT INTO schema_migrations VALUES(108,?,0)', ('future_schema',))
        elif damage == 'column':
            conn.execute('ALTER TABLE recordings RENAME COLUMN title TO changed_title')
        else:
            name, owner = conn.execute("SELECT name,tbl_name FROM sqlite_schema WHERE type='trigger' "
                                       "ORDER BY name LIMIT 1").fetchone()
            quote = lambda value: '"'+value.replace('"', '""')+'"'
            conn.execute('DROP TRIGGER '+quote(name))
            conn.execute('CREATE TRIGGER '+quote(name)+' AFTER INSERT ON '+quote(owner)+' BEGIN SELECT 1; END')
        conn.commit()
        conn.execute('PRAGMA query_only=ON')
        with pytest.raises(MigrationError):
            version = schema_admission.history(conn)
            schema_admission.validate_version(conn, version, foreign_keys=False, data=False)
