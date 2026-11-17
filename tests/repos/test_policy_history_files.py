"""Exact SQL archive receipts are independent of file contents and paths."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.repos import policy_history_files as writes


@pytest.mark.parametrize('changed', ['file_id', 'version', 'digest', 'size'])
def test_only_the_reserved_archive_can_become_a_ready_reference(changed):
    with closing(sqlite3.connect(':memory:')) as conn:
        record = dict(version='v1', digest='a' * 64, size=12)
        with conn:
            identity = writes.reserve(conn, record)
        record['file_id'] = identity
        original = record.copy()
        record[changed] = dict(file_id=identity+1, version='other', digest='b' * 64, size=13)[changed]
        assert writes.reference(conn, 'v1') is None
        with pytest.raises(ValueError, match='reservation changed'), conn:
            writes.complete(conn, record)
        assert writes.reference(conn, 'v1') is None
        with conn:
            writes.complete(conn, original)
        assert writes.reference(conn, 'v1') == dict(id=identity, content_digest='a' * 64, byte_size=12)
