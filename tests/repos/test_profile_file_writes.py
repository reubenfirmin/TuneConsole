"""Profile receipt identity and rollback on minimal SQL owners."""
from contextlib import closing
import sqlite3
from threading import RLock
from types import SimpleNamespace

import pytest

from yt_playlist.core import profile_files
from yt_playlist.core.db_diagnostics import DiagnosticConnection
from yt_playlist.core import file_receipts
from tests.store_fixture_support import install_file_receipts
from yt_playlist.repos import profile_file_writes as writes
from yt_playlist.repos.musical_profiles import MusicalProfilesRepo


@pytest.fixture
def conn(tmp_path):
    with closing(sqlite3.connect(tmp_path / 'profiles.db', factory=DiagnosticConnection)) as conn:
        conn.execute('CREATE TABLE recordings(id INTEGER PRIMARY KEY,identity_key TEXT UNIQUE)')
        conn.execute('CREATE TABLE rec_musical_profiles(identity_key TEXT PRIMARY KEY,payload TEXT,version TEXT,digest TEXT,prepared_at REAL)')
        conn.execute('CREATE TABLE rec_musical_source_profiles(source_key TEXT PRIMARY KEY,payload TEXT,version TEXT,digest TEXT,prepared_at REAL)')
        profile_files.install(conn)
        file_receipts.install_profile(conn)
        file_receipts.validate_profile(conn)
        install_file_receipts(conn)
        yield conn


@pytest.mark.parametrize('changed', ['id', 'kind', 'digest', 'size'])
def test_mismatched_pack_cannot_complete_a_pending_receipt(conn, changed):
    with conn:
        identity, = writes.reserve(conn, [dict(kind='recording', digest=b'x'*32, size=100)])
    reference = ['song', 1, b'x'*32, 2, 'v', 'digest', identity, 100]
    if changed == 'id':
        reference[6] += 1
    elif changed == 'digest':
        reference[2] = b'y'*32
    elif changed == 'size':
        reference[7] += 1
    with pytest.raises(ValueError), conn:
        writes.complete(conn, 'source' if changed == 'kind' else 'recording', [reference])
    assert conn.execute("SELECT file_status FROM collection_file_writes WHERE kind='profile'").fetchone()[0] == 'pending'


def test_reservation_is_committed_and_receipt_update_rolls_back_with_heads(conn):
    repo = MusicalProfilesRepo(SimpleNamespace(conn=conn, _lock=RLock()))
    identity, = repo.reserve_files([dict(kind='recording', digest=b'x'*32, size=100)])
    with closing(sqlite3.connect(conn.execute('PRAGMA database_list').fetchone()[2])) as observer:
        assert observer.execute("SELECT file_address,file_status FROM collection_file_writes "
                                "WHERE kind='profile'").fetchone() == (identity, 'pending')
    assert not conn.in_transaction
    reference = ('song', 1, b'x'*32, 2, 'v', 'digest', identity, 100)
    with pytest.raises(ValueError, match='Stale'), conn:
        writes.complete(conn, 'recording', [reference])
        profile_files.install_references(conn, 'recording', [reference])
    assert conn.execute("SELECT file_status FROM collection_file_writes WHERE kind='profile'").fetchone()[0] == 'pending'
    with conn:
        conn.execute("INSERT INTO recordings VALUES (1,'song')")
        conn.execute("INSERT INTO rec_musical_profiles VALUES ('song','{}','v','digest',1)")
        writes.complete(conn, 'recording', [reference])
        profile_files.install_references(conn, 'recording', [reference])
    assert conn.execute('SELECT file_id FROM recording_profile_files').fetchone()[0] == identity
    assert conn.execute('SELECT payload FROM rec_musical_profiles').fetchone()[0] == ''
