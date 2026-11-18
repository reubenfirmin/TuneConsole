"""Retained backup completion must match its exact pending SQL owner."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.repos import action_backup_writes as writes


@pytest.mark.parametrize('changed', ['identity', 'action', 'position', 'digest', 'size'])
def test_completion_rejects_another_owner_or_body(changed):
    with closing(sqlite3.connect(':memory:')) as conn:
        with conn:
            identity = writes.reserve(conn, 7, 0, ('a' * 64, 12))
        values = dict(identity=identity, action=7, position=0, digest='a' * 64, size=12)
        values[changed] = dict(identity=identity + 1, action=8, position=1, digest='b' * 64, size=13)[changed]
        with pytest.raises(ValueError, match='reservation changed'), conn:
            writes.complete(conn, values['identity'], values['action'], values['position'],
                            ('file', values['digest'], values['size']))
        assert conn.execute('SELECT file_status FROM action_backup_writes').fetchone() == ('pending',)
        with conn:
            writes.complete(conn, identity, 7, 0, ('file', 'a' * 64, 12))
        assert conn.execute('SELECT file_status FROM action_backup_writes').fetchone() == ('ready',)
