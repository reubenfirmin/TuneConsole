"""Only exact pending policy files can be completed."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.repos import policy_file_writes as writes


@pytest.mark.parametrize('changed', ['file_id', 'body', 'version', 'digest', 'size', 'filename', 'offset'])
def test_completion_requires_the_reserved_policy_and_file(changed):
    with closing(sqlite3.connect(':memory:')) as conn:
        record = dict(body=True, version='base', digest='a' * 64, size=12)
        with conn:
            identity = writes.reserve(conn, record)
        record |= dict(file_id=identity, filename=f'/policies/by-id/{identity % 256:02x}/{identity}.json', offset=0)
        original = record.copy()
        record[changed] = dict(file_id=identity+1, body=False, version='other', digest='b' * 64, size=13,
                               filename='/policies/by-id/02/2.json', offset=42)[changed]
        with pytest.raises(ValueError, match='reserved identity|reservation changed'), conn:
            writes.complete(conn, record)
        assert conn.execute('SELECT file_status FROM policy_file_writes').fetchone() == ('pending',)
        with conn:
            writes.complete(conn, original)
        assert conn.execute('SELECT file_status FROM policy_file_writes').fetchone() == ('ready',)
