"""Paid result receipts are bound to their request, attempt and exact bytes."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.repos import description_file_writes as writes


@pytest.mark.parametrize('changed', ['id', 'request', 'attempt', 'digest', 'size'])
def test_a_changed_result_cannot_complete_the_reserved_file(changed):
    with closing(sqlite3.connect(':memory:')) as conn:
        with conn:
            identity = writes.reserve(conn, 7, 2, 'a' * 64, 12)
        values = dict(id=identity, request=7, attempt=2, digest='a' * 64, size=12)
        values[changed] = dict(id=identity + 1, request=8, attempt=3, digest='b' * 64, size=13)[changed]
        reference = (f'/paid/by-id/{identity % 256:02x}/{identity}.json', values['digest'], values['size'])
        with pytest.raises(ValueError), conn:
            writes.complete(conn, values['id'], values['request'], values['attempt'], reference)
        assert conn.execute('SELECT file_status FROM description_file_writes').fetchone() == ('pending',)
        with conn:
            writes.complete(conn, identity, 7, 2, (reference[0], 'a' * 64, 12))
        assert conn.execute('SELECT file_status FROM description_file_writes').fetchone() == ('ready',)
