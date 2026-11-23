"""Recommendation file identities commit pending and require exact completion."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.core import file_receipts
from yt_playlist.core.migrations import recommendation_receipt_storage
from yt_playlist.repos import recommendation_file_writes as writes


@pytest.mark.parametrize('changed', ['identity', 'owner', 'surface', 'key', 'digest', 'size'])
def test_completion_requires_the_reserved_record_and_body(changed):
    with closing(sqlite3.connect(':memory:')) as conn:
        file_receipts.collection.install(conn)
        recommendation_receipt_storage.install_guards(conn)
        with conn:
            identity = writes.reserve(conn, 'home_display_files', 'home_prepared', '0', ('a' * 64, 12))
        values = dict(identity=identity, owner='home_display_files', surface='home_prepared',
                      key='0', digest='a' * 64, size=12)
        values[changed] = dict(identity=identity + 1, owner='listening_output_files', surface='home_tuned',
                              key='1', digest='b' * 64, size=13)[changed]
        with pytest.raises(ValueError, match='reservation changed'), conn:
            writes.complete(conn, values['identity'], values['owner'], values['surface'], values['key'],
                            (f'/files/by-id/{identity % 256:02x}/{identity}.json',
                             values['digest'], values['size']))
        assert conn.execute('SELECT file_status FROM collection_file_writes').fetchone() == ('pending',)
        with conn:
            writes.complete(conn, identity, 'home_display_files', 'home_prepared', '0',
                            (f'/files/by-id/{identity % 256:02x}/{identity}.json', 'a' * 64, 12))
        assert conn.execute('SELECT file_status FROM collection_file_writes').fetchone() == ('ready',)


@pytest.mark.parametrize('filename', ['/files/by-id/02/2.json', '/files/by-id/00/1.json',
                                      '/files/aa/' + 'a' * 64 + '.json'])
def test_completion_rejects_crossed_or_noncanonical_file_receipts(filename):
    with closing(sqlite3.connect(':memory:')) as conn:
        file_receipts.collection.install(conn)
        recommendation_receipt_storage.install_guards(conn)
        with conn:
            identity = writes.reserve(conn, 'home_display_files', 'home_prepared', '0', ('a' * 64, 12))
            other = writes.reserve(conn, 'home_display_files', 'home_prepared', '0', ('a' * 64, 12))
        assert (identity, other) == (1, 2)
        with pytest.raises(ValueError, match='reservation changed'), conn:
            writes.complete(conn, identity, 'home_display_files', 'home_prepared', '0', (filename, 'a' * 64, 12))
        assert conn.execute('SELECT file_status FROM collection_file_writes ORDER BY id').fetchall() == [
            ('pending',), ('pending',)]
