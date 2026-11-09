"""Independent preparation admission against the unchanged historical installers."""
from contextlib import ExitStack, closing
import sqlite3

import pytest

from tests.genre_catalog_support import use_shipped_catalog
from tests.store_fixture_support import install_numbered_input
from yt_playlist.core import schema_admission
from yt_playlist.core.migrations import runner
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.schema_contract import MigrationError


@pytest.fixture
def disable_periodic_library_sync():
    # These readers never construct an HTTP application.
    pass


HISTORICAL_INPUTS = (79, 93, 99, 102, 103, 104, 107)


@pytest.fixture(scope='module')
def frozen_admission_input():
    # Admission consumes the frozen v79 SQL and real subsequent installers.
    # Full replay from version one remains an independent oracle below.
    templates = {}
    with ExitStack() as resources:
        def template(version):
            if version not in templates:
                conn = resources.enter_context(closing(sqlite3.connect(':memory:')))
                with pytest.MonkeyPatch.context() as patch:
                    use_shipped_catalog(patch)
                    install_numbered_input(conn, 79)
                    runner.migrate(conn, registry=MIGRATIONS[:version])
                conn.execute('PRAGMA query_only=ON')
                templates[version] = conn
            return templates[version]

        yield template


@pytest.mark.parametrize('version', HISTORICAL_INPUTS)
def test_historical_admission_input_keeps_real_replay_and_exact_history(
        version, _pristine_installed_schema, frozen_admission_input):
    source = _pristine_installed_schema(version)
    assert source.execute('PRAGMA query_only').fetchone()[0] == 1
    assert [tuple(row) for row in source.execute(
        'SELECT version,name FROM schema_migrations ORDER BY version')] == [
            (migration.version, migration.name) for migration in MIGRATIONS[:version]]
    assert not source.execute('PRAGMA foreign_key_check').fetchone()
    definitions = ("SELECT type,name,tbl_name,sql FROM sqlite_schema "
                   "WHERE sql IS NOT NULL ORDER BY type,name")
    assert [tuple(row) for row in frozen_admission_input(version).execute(definitions)] == [
        tuple(row) for row in source.execute(definitions)]


@pytest.mark.parametrize('version', [79, 80, 81, 82, 83, 84, 85, 86, 88, 91, 92, 93, 99, 104, 107])
def test_preparation_admits_frozen_installed_schemas_without_writing(version, frozen_admission_input):
    with closing(sqlite3.connect(':memory:')) as conn, conn:
        conn.row_factory = sqlite3.Row
        frozen_admission_input(version).backup(conn)
        conn.execute('PRAGMA query_only=ON')
        changes = conn.total_changes
        assert schema_admission.history(conn) == version
        schema_admission.validate_version(conn, version, foreign_keys=False, data=False)
        assert conn.total_changes == changes


@pytest.mark.parametrize('damage', ['history_name', 'history_gap', 'future', 'column', 'trigger'])
def test_preparation_rejects_changed_history_and_owner_definitions(damage, frozen_admission_input):
    with closing(sqlite3.connect(':memory:')) as conn, conn:
        conn.row_factory = sqlite3.Row
        frozen_admission_input(79).backup(conn)
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
