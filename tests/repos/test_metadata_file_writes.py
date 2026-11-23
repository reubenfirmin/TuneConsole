"""SQL receipts preserve incomplete work and bind selected metadata revisions."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.core.metadata_file_contract import describe
from tests.store_fixture_support import install_file_receipts
from yt_playlist.repos import metadata_file_writes as writes


@pytest.mark.parametrize('changed', ['file_id', 'kind', 'owner', 'revision', 'digest', 'size'])
def test_changed_receipt_cannot_replace_the_selected_revision(changed):
    with closing(sqlite3.connect(':memory:')) as conn:
        install_file_receipts(conn)
        original = describe('choices', (7,), 1, {'value': 'Jazz'})
        with conn:
            original['file_id'] = writes.reserve(conn, original)
        altered = {**original, changed: dict(file_id=99, kind='conflicts', owner=(8,), revision=2,
                                            digest='0' * 64, size=original['size'] + 1)[changed]}
        with pytest.raises(ValueError), conn:
            writes.complete(conn, altered, kind='choices', owner=(7,), revision=1)
        assert writes.reference(conn, 'choices', (7,), 1) is None
        assert conn.execute("SELECT file_status FROM collection_file_writes WHERE kind='runtime-metadata'").fetchone() == ('pending',)
        with conn:
            writes.complete(conn, original, kind='choices', owner=(7,), revision=1)
        assert writes.reference(conn, 'choices', (7,), 1) == (original['file_id'], original['digest'], original['size'])


def test_committed_pending_is_visible_and_failed_completion_can_retry(tmp_path):
    path = tmp_path / 'receipts.db'
    with closing(sqlite3.connect(path)) as conn, closing(sqlite3.connect(path)) as observer:
        install_file_receipts(conn)
        record = describe('native', ('source',), 1, {'value': 0.8})
        with conn:
            record['file_id'] = writes.reserve(conn, record)
        assert observer.execute("SELECT file_address,file_status FROM collection_file_writes "
                                "WHERE kind='runtime-metadata'").fetchone() == (record['file_id'], 'pending')
        with pytest.raises(RuntimeError), conn:
            writes.complete(conn, record, kind='native', owner=('source',), revision=1)
            raise RuntimeError('head update interrupted')
        assert writes.reference(observer, 'native', ('source',), 1) is None
        with conn:
            writes.complete(conn, record, kind='native', owner=('source',), revision=1)
        assert writes.reference(observer, 'native', ('source',), 1)[0] == record['file_id']


def test_abandoned_choice_identity_is_not_reused():
    from yt_playlist.core import runtime_metadata_choices

    with closing(sqlite3.connect(':memory:')) as conn:
        install_file_receipts(conn)
        conn.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        with conn:
            first = runtime_metadata_choices.next_identity(conn)
            writes.reserve(conn, describe('choices', (first,), 1, {'value': 'abandoned'}))
        assert runtime_metadata_choices.next_identity(conn) > first
