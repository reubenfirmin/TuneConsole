"""Routine schema checks inspect the named owner without expanding other views."""
import sqlite3

import pytest

from yt_playlist.core.migrations import current


@pytest.mark.parametrize('options,expected', [('', (0, 0)), ('WITHOUT ROWID', (1, 0)),
    ('STRICT', (0, 1)), ('WITHOUT ROWID, STRICT', (1, 1)), ('STRICT, WITHOUT ROWID', (1, 1))])
def test_table_flags_ignore_unrelated_main_and_temp_views(options, expected):
    conn = sqlite3.connect(':memory:')
    try:
        sql = ('''CREATE TABLE target (id INTEGER PRIMARY KEY,"strict" TEXT,
            note TEXT DEFAULT 'WITHOUT ROWID',CHECK(length(note)>=0)) '''+options)
        conn.execute(sql)
        assert tuple(conn.execute('PRAGMA main.table_list(target)').fetchone()[4:]) == expected
        conn.executescript('''CREATE TABLE unrelated(secret TEXT);
            CREATE VIEW unrelated_main AS SELECT secret FROM unrelated;
            CREATE TEMP VIEW unrelated_temp AS SELECT secret FROM main.unrelated;''')
        unrelated_reads = []
        def authorize(operation, owner, *unused):
            if operation == sqlite3.SQLITE_READ and owner == 'unrelated':
                unrelated_reads.append(owner)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        assert current._table(conn, 'target', sql)[4] == expected
        assert not unrelated_reads, 'A named table check expanded an unrelated view'
    finally:
        conn.close()
