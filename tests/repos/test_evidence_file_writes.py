"""Exact SQL receipts and selected heads without any file fixture."""
from contextlib import closing
import sqlite3

import pytest

from tests.store_fixture_support import install_file_receipts
from yt_playlist.repos import evidence_file_writes as writes


@pytest.mark.parametrize('changed', ['id', 'kind', 'key', 'digest', 'size'])
def test_completion_cannot_select_changed_evidence(changed):
    with closing(sqlite3.connect(':memory:')) as conn:
        with conn:
            install_file_receipts(conn)
            reserved = writes.reserve(conn, {('response', 'event'): ('a' * 64, 10)})
        identity, digest, size = reserved['response', 'event']
        values = dict(id=identity, kind='response', key='event', digest=digest, size=size)
        values[changed] = dict(id=identity + 1, kind='forecast', key='different', digest='b' * 64, size=11)[changed]
        with pytest.raises(ValueError), conn:
            writes.complete(conn, {(values['kind'], values['key']): (values['id'], values['digest'], values['size'])}, {})
        assert writes.references(conn) == {}
        assert conn.execute("SELECT file_status FROM collection_file_writes WHERE kind='listening-evidence'").fetchone() == ('pending',)
        with conn:
            writes.complete(conn, reserved, {})
        assert writes.references(conn) == reserved


def test_failed_batch_completion_leaves_all_records_pending_and_retryable(tmp_path):
    path = tmp_path / 'evidence.db'
    with closing(sqlite3.connect(path)) as conn, closing(sqlite3.connect(path)) as observer:
        with conn:
            install_file_receipts(conn)
            reserved = writes.reserve(conn, {('artifact', 'context'): ('a' * 64, 10), ('opportunity', 'menu'): ('b' * 64, 11)})
        assert observer.execute("SELECT file_status FROM collection_file_writes "
                                "WHERE kind='listening-evidence'").fetchall() == [('pending',), ('pending',)]
        with pytest.raises(RuntimeError), conn:
            writes.complete(conn, reserved, {})
            raise RuntimeError('selection interrupted')
        assert writes.references(observer) == {}
        with conn:
            writes.complete(conn, reserved, {})
        assert writes.references(observer) == reserved
        with conn:
            replacement = writes.reserve(conn, {('artifact', 'context'): ('c' * 64, 12)})
            writes.complete(conn, replacement, reserved)
        assert writes.references(observer) == {**reserved, **replacement}
        assert conn.execute("SELECT selected FROM collection_file_writes WHERE kind='listening-evidence' AND file_address=?",
                            (reserved['artifact', 'context'][0],)).fetchone() == (0,)
