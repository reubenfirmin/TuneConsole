"""Exact pack reservation completion on tiny SQL owners, without file access."""
from contextlib import closing
import sqlite3
from threading import RLock
from types import SimpleNamespace

import pytest

from yt_playlist.core import embedding_files
from yt_playlist.core.db_diagnostics import DiagnosticConnection
from yt_playlist.repos import embedding_file_writes as writes
from yt_playlist.repos.embeddings import EmbeddingRepo


@pytest.fixture
def conn(tmp_path):
    with closing(sqlite3.connect(tmp_path / 'embeddings.db', factory=DiagnosticConnection)) as conn:
        for kind in ('recording', 'artist'):
            conn.execute(f'CREATE TABLE {kind}s(id INTEGER PRIMARY KEY)')
            conn.execute(f'CREATE TABLE {kind}_embeddings({kind}_id INTEGER,space TEXT,model_version TEXT,dimensions INTEGER,vector BLOB,built_at REAL)')
            conn.execute(f'CREATE TABLE {kind}_redirects(retired_id INTEGER)')
        embedding_files.install(conn)
        writes.install(conn)
        writes.validate(conn)
        yield conn


@pytest.mark.parametrize('kind', ['recording', 'artist'])
def test_incompatible_head_still_fails_schema_validation(conn, kind):
    conn.execute(f'ALTER TABLE {kind}_embedding_files ADD COLUMN unexpected INTEGER')
    with pytest.raises(writes.MigrationError, match='Incompatible embedding file identity owner'):
        writes.validate(conn)


@pytest.mark.parametrize('changed', ['id', 'kind', 'dimensions', 'digest', 'positions'])
def test_changed_pack_cannot_complete_a_reservation(conn, changed):
    record = dict(kind='recording', dimensions=2, digest=b'x'*32, size=40)
    with conn:
        identity, = writes.reserve(conn, [record])
    kind = 'artist' if changed == 'kind' else 'recording'
    rows = [(1, 2, b'x'*32, 0, 1, 10, identity), (2, 2, b'x'*32, 1, 2, 10, identity)]
    if changed == 'id':
        rows = [(*row[:6], identity+1) for row in rows]
    elif changed == 'dimensions':
        rows = [(row[0], 3, *row[2:]) for row in rows]
    elif changed == 'digest':
        rows = [(*row[:2], b'y'*32, *row[3:]) for row in rows]
    elif changed == 'positions':
        rows = [rows[0], (*rows[1][:3], 0, *rows[1][4:])]
    with pytest.raises(ValueError), conn:
        writes.complete(conn, kind, rows)
    assert conn.execute('SELECT file_status FROM embedding_file_writes').fetchone()[0] == 'pending'


def test_ready_publication_binds_the_exact_pack_id_and_rolls_back_on_bad_subject(conn):
    conn.execute('PRAGMA foreign_keys=ON')
    with conn:
        conn.execute('INSERT INTO recordings VALUES (1)')
        identity, = writes.reserve(conn, [dict(kind='recording', dimensions=2, digest=b'x'*32, size=32)])
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute('BEGIN')
        embedding_files.publish_prepared(conn, 'recording', 'collaborative', 'v1', [(2, 2, b'x'*32, 0, 1, 10, identity)])
    assert conn.execute('SELECT file_status FROM embedding_file_writes').fetchone()[0] == 'pending'
    with conn:
        conn.execute('BEGIN')
        embedding_files.publish_prepared(conn, 'recording', 'collaborative', 'v1', [(1, 2, b'x'*32, 0, 1, 10, identity)])
    assert conn.execute('SELECT file_id FROM recording_embedding_files').fetchone()[0] == identity
    assert conn.execute('SELECT file_status FROM embedding_file_writes').fetchone()[0] == 'ready'


def test_reservation_commits_before_control_returns_to_the_file_writer(conn):
    repo = EmbeddingRepo(SimpleNamespace(conn=conn, _lock=RLock()))
    identity, = repo.reserve_files([dict(kind='artist', dimensions=2, digest=b'x'*32, size=32)])
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    with closing(sqlite3.connect(path)) as observer:
        assert observer.execute('SELECT id,kind,file_status FROM embedding_file_writes').fetchone() == (identity, 'artist', 'pending')
    assert not conn.in_transaction
