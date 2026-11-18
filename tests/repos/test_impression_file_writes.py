"""Served diagnostic status stays bound to its reserved impression identity."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.repos import impression_file_writes as writes


@pytest.mark.parametrize('changed', ['id', 'request', 'surface', 'rank'])
def test_changed_impression_cannot_complete_a_pending_diagnostic(changed):
    with closing(sqlite3.connect(':memory:')) as conn:
        with conn:
            writes.install(conn)
            writes.reserve(conn, 7, 'request', 'home', 1)
        values = dict(id=7, request='request', surface='home', rank=1)
        values[changed] = dict(id=8, request='different', surface='radio', rank=2)[changed]
        with pytest.raises(ValueError), conn:
            writes.complete(conn, *values.values())
        assert conn.execute('SELECT file_status FROM impression_file_writes').fetchone() == ('pending',)
        with conn:
            writes.complete(conn, 7, 'request', 'home', 1)
        assert conn.execute('SELECT file_status FROM impression_file_writes').fetchone() == ('ready',)
