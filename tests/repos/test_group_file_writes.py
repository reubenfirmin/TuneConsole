"""A group file receipt completes only for its reserved kind and content."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.core.group_documents import HEADER
from yt_playlist.repos import group_file_writes as writes


@pytest.mark.parametrize('changed', ['identity', 'kind', 'digest', 'size', 'offset'])
def test_only_the_reserved_group_document_can_become_ready(changed):
    with closing(sqlite3.connect(':memory:')) as conn:
        with conn:
            identity, = writes.reserve(conn, [dict(kind='lineage', digest='a'*64, size=12)])
        values = dict(identity=identity, kind='lineage', digest='a'*64, size=12, offset=len(HEADER))
        values[changed] = dict(identity=identity+1, kind='definition', digest='b'*64, size=13, offset=0)[changed]
        reference = (f'/groups/by-id/{values["identity"] % 256:02x}/{values["identity"]}.json',
                     values['digest'], values['size'], values['offset'])
        with pytest.raises(ValueError), conn:
            writes.complete(conn, values['kind'], reference)
        assert conn.execute('SELECT file_status FROM group_file_writes').fetchone() == ('pending',)
        with conn:
            writes.complete(conn, 'lineage', (f'/groups/by-id/{identity % 256:02x}/{identity}.json', 'a'*64, 12, len(HEADER)))
        assert conn.execute('SELECT file_status FROM group_file_writes').fetchone() == ('ready',)
