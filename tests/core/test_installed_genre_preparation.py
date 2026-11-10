"""Old genre repairs preserve invalid history while independently guarding writes."""
import sqlite3
from contextlib import closing

import pytest

from tests.core.test_installed_genre_contract import earlier_database
from tests.core.test_domain_contract_preparation import finish
from tests.genre_catalog_support import bound_catalog
from yt_playlist.core import schema_inspection
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import domain_contracts, installed_genre_contract as repair


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """SQL preparation runs without starting the HTTP application."""


def test_regular_repair_never_reads_or_copies_axis_population_and_audit_is_separate(tmp_path, monkeypatch):
    bound_catalog(monkeypatch)
    with closing(earlier_database(tmp_path/'earlier.db')) as conn, schema_inspection.reuse_tables(conn):
        conn.execute('PRAGMA synchronous=OFF')
        with conn:
            conn.execute("INSERT INTO taste_axes(id,kind) VALUES (8,'other')")
        history = list(map(tuple,conn.execute('SELECT * FROM schema_migrations')))
        rows = list(map(tuple,conn.execute('SELECT * FROM taste_axes ORDER BY id')))
        sql = conn.execute("SELECT sql FROM sqlite_schema WHERE name='taste_axes'").fetchone()[0]
        def deny(action,owner,column,*rest):
            return sqlite3.SQLITE_DENY if action in (sqlite3.SQLITE_READ,sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE) and owner=='taste_axes' else sqlite3.SQLITE_OK
        conn.set_authorizer(deny)
        try:
            regular.migrate(conn)
        finally:
            conn.set_authorizer(None)
        assert list(map(tuple,conn.execute('SELECT * FROM taste_axes ORDER BY id')))==rows
        assert conn.execute("SELECT sql FROM sqlite_schema WHERE name='taste_axes'").fetchone()[0]==sql
        assert list(map(tuple,conn.execute('SELECT * FROM schema_migrations')))==history
        assert conn.execute("SELECT genre_key FROM taste_axes WHERE id=7").fetchone()[0]=='jazz'
        assert conn.execute('SELECT lean_value FROM taste_axis_legacy').fetchone()[0] is None
        with pytest.raises(sqlite3.IntegrityError,match='Invalid future taste axis'):
            with conn:
                conn.execute("INSERT INTO taste_axes(id,kind) VALUES (9,'other')")
        selected = repair.job(conn)
        selected.install(conn)
        finish(selected,conn)
        with pytest.raises(MigrationError,match='Invalid historical taste_axes'):
            domain_contracts.verify(conn,selected)
        record = conn.execute('SELECT * FROM '+domain_contracts.table(selected,'rows')+
            ' WHERE violation_count>0').fetchone()
        original = domain_contracts.original(conn,selected,record['source_key'],record['source_digest'])
        assert original['fields']['value']=={'type':'null','value':None}
        with conn:
            conn.execute("UPDATE taste_axes SET value='preserved-unknown' WHERE id=8")
        finish(selected,conn)
        domain_contracts.verify(conn,selected)
        assert domain_contracts.original(conn,selected,record['source_key'],record['source_digest'])==original
