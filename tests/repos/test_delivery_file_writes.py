"""A response receipt can only complete for the exact admitted delivery."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.repos import delivery_file_writes as writes


@pytest.mark.parametrize('changed', ['id', 'contract', 'digest', 'size', 'filename', 'offset', 'owner'])
def test_completion_checks_the_receipt_and_the_owning_delivery(changed):
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.execute('CREATE TABLE metadata_deliveries(id INTEGER PRIMARY KEY,contract_digest TEXT,'
                     'raw_filename TEXT,raw_digest TEXT,raw_byte_size INTEGER,raw_offset INTEGER)')
        with conn:
            identity = writes.reserve(conn, 'a' * 64, 'b' * 64, 12)
            filename = f'/metadata/by-id/{identity % 256:02x}/{identity}.json'
            conn.execute('INSERT INTO metadata_deliveries VALUES (7,?,?,?,?,0)', ('a' * 64, filename, 'b' * 64, 12))
        values = dict(id=identity, contract='a' * 64, digest='b' * 64, size=12, filename=filename, offset=0, owner=7)
        values[changed] = dict(id=identity + 1, contract='c' * 64, digest='c' * 64, size=13,
                              filename='/metadata/by-id/02/2.json', offset=10, owner=8)[changed]
        with pytest.raises(ValueError), conn:
            writes.complete(conn, values['id'], values['contract'], values['owner'],
                            (values['filename'], values['digest'], values['size'], values['offset']))
        assert conn.execute('SELECT file_status FROM delivery_file_writes').fetchone() == ('pending',)
        with conn:
            writes.complete(conn, identity, 'a' * 64, 7, (filename, 'b' * 64, 12, 0))
        assert conn.execute('SELECT file_status,delivery_id FROM delivery_file_writes').fetchone() == ('ready', 7)


def test_receipt_cannot_adopt_a_delivery_with_different_captured_bytes():
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.execute('CREATE TABLE metadata_deliveries(id INTEGER PRIMARY KEY,contract_digest TEXT,'
                     'raw_filename TEXT,raw_digest TEXT,raw_byte_size INTEGER,raw_offset INTEGER)')
        with conn:
            identity = writes.reserve(conn, 'a' * 64, 'b' * 64, 12)
            filename = f'/metadata/by-id/{identity % 256:02x}/{identity}.json'
            conn.execute('INSERT INTO metadata_deliveries VALUES (7,?,?,?,?,0)', ('a' * 64, filename, 'c' * 64, 12))
        with pytest.raises(ValueError), conn:
            writes.complete(conn, identity, 'a' * 64, 7, (filename, 'b' * 64, 12, 0))
        assert conn.execute('SELECT file_status FROM delivery_file_writes').fetchone() == ('pending',)
