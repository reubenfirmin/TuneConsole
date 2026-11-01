"""Old genre repairs preserve invalid history while independently guarding writes."""
from dataclasses import replace
import sqlite3

import pytest

from tests.core.test_installed_genre_contract import earlier_database
from tests.core.test_domain_contract_preparation import finish
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import domain_contracts, installed_genre_contract as repair


def test_regular_repair_never_reads_or_copies_axis_population_and_audit_is_separate(tmp_path):
    conn = earlier_database(tmp_path/'earlier.db')
    try:
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
    finally:
        conn.close()


def test_interrupted_audit_retains_commits_and_full_historical_upgrade_preserves_guards(tmp_path):
    conn = earlier_database(tmp_path/'upgrade.db')
    try:
        regular.migrate(conn)
        selected = repair.job(conn)
        selected.install(conn)
        checkpoint = finish(selected,conn)
        def fail(*args):
            selected.copy(*args)
            raise OSError('controlled audit failure')
        # A captured source change ensures there is another bounded batch.
        with conn:
            conn.execute("UPDATE taste_axes SET genre_key='rock' WHERE id=7")
        with pytest.raises(OSError,match='controlled audit failure'):
            replace(selected,copy=fail).step(conn)
        assert selected.progress(conn).status=='failed'
        assert selected.progress(conn).processed==checkpoint.processed
        regular.migrate(conn)
        selected.step(conn,retry=True)
        finish(selected,conn)
        runner.migrate(conn,registry=registry.MIGRATIONS)
        regular.migrate(conn)
        selected.validate(conn)
        finish(selected,conn)
        domain_contracts.verify(conn,selected)
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute("INSERT INTO taste_axes(kind) VALUES ('other')")
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        conn.close()
