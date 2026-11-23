"""Pending prediction pack receipts and final SQL rollback on tiny owners."""
from contextlib import closing
import sqlite3
from threading import RLock
from types import SimpleNamespace

import pytest

from tests.store_fixture_support import install_file_receipts
from yt_playlist.core import destination_schema
from yt_playlist.core.db_diagnostics import DiagnosticConnection
from yt_playlist.repos import provenance_file_writes as writes
from yt_playlist.repos.vibe_predictions import VibePredictionsRepo


@pytest.fixture
def conn(tmp_path):
    with closing(sqlite3.connect(tmp_path / 'predictions.db', factory=DiagnosticConnection)) as conn:
        install_file_receipts(conn)
        for sql in destination_schema.statements():
            if sql.startswith(('CREATE TABLE "predictions" ', 'CREATE TRIGGER predictions_provenance_immutable ')):
                conn.execute(sql)
        yield conn


@pytest.mark.parametrize('change', ['file_id', 'digest', 'size'])
def test_mismatched_pack_cannot_complete_a_pending_receipt(conn, change):
    with conn:
        identity, = writes.reserve(conn, [{'digest': b'x' * 32, 'size': 10}])
    record = {'file_id': identity, 'digest': b'x' * 32, 'size': 10}
    record[change] = b'y' * 32 if change == 'digest' else record[change] + 1
    with pytest.raises(ValueError), conn:
        writes.complete(conn, [record])
    assert conn.execute("SELECT file_status FROM collection_file_writes WHERE kind='prediction-provenance'").fetchone()[0] == 'pending'


def test_reservation_commits_before_files_and_completion_rolls_back_with_its_heads(conn):
    repo = VibePredictionsRepo(SimpleNamespace(conn=conn, _lock=RLock()))
    identity, = repo.reserve_provenance([{'digest': b'x' * 32, 'size': 10}])
    assert not conn.in_transaction
    with closing(sqlite3.connect(conn.execute('PRAGMA database_list').fetchone()[2])) as observer:
        assert observer.execute("SELECT file_address,file_status FROM collection_file_writes "
                                "WHERE kind='prediction-provenance'").fetchone() == (identity, 'pending')
    record = {'file_id': identity, 'digest': b'x' * 32, 'size': 10}
    with pytest.raises(OSError), conn:
        writes.complete(conn, [record])
        raise OSError('interrupted head update')
    assert conn.execute("SELECT file_status FROM collection_file_writes WHERE kind='prediction-provenance'").fetchone()[0] == 'pending'
    with conn:
        writes.complete(conn, [record])
        conn.execute("INSERT INTO predictions(id,recording_id,vibe,score,support,kind,policy_version_id,"
                     "provenance_fingerprint,provenance_bytes,provenance_state,provenance_file_id) "
                     "VALUES (1,1,'relax',.5,1,'artist',1,?,2,'ready',?)", (b'y' * 32, identity))
    assert repo.provenance_owners(identity, None) == (True, None)
    assert repo.provenance_owners(None, 0) == (set(), set())
    with pytest.raises(sqlite3.IntegrityError, match='immutable'), conn:
        conn.execute('UPDATE predictions SET provenance_file_id=NULL')
