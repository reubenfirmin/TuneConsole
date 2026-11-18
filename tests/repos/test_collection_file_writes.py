"""SQL reservations reject a different body or owner without file access."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.repos import collection_file_writes as writes


@pytest.mark.parametrize('interrupted', [False, True])
def test_deferred_admission_creates_only_the_empty_owner_in_the_reservation_transaction(interrupted):
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.execute('CREATE TABLE installed_state(value TEXT)')
        conn.execute("INSERT INTO installed_state VALUES ('retained')")
        conn.commit()
        conn.execute('BEGIN IMMEDIATE')
        identity = writes.reserve(conn, 'radio-session', 'episode', ('a' * 64, 12))
        if interrupted:
            conn.rollback()
        else:
            conn.commit()
            writes.validate(conn)
            assert conn.execute('SELECT file_status FROM collection_file_writes WHERE id=?',
                                (identity,)).fetchone() == ('pending',)
        assert bool(conn.execute("SELECT 1 FROM sqlite_schema WHERE name='collection_file_writes'").fetchone()) is not interrupted
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='schema_changes'").fetchone() is None
        assert conn.execute('SELECT value FROM installed_state').fetchone() == ('retained',)


@pytest.mark.parametrize('changed', ['identity', 'kind', 'owner', 'digest', 'size'])
def test_completion_requires_the_exact_pending_identity_and_content(changed):
    with closing(sqlite3.connect(':memory:')) as conn:
        with conn:
            writes.install(conn)
            identity = writes.reserve(conn, 'radio-session', 'episode', ('a' * 64, 12))
        values = dict(identity=identity, kind='radio-session', owner='episode', digest='a' * 64, size=12)
        values[changed] = dict(identity=identity + 1, kind='library-sync', owner='another-episode',
                               digest='b' * 64, size=13)[changed]
        with pytest.raises(ValueError, match='reservation changed'), conn:
            writes.complete(conn, values['identity'], values['kind'], values['owner'],
                            (f'/files/by-id/{identity % 256:02x}/{identity}.json',
                             values['digest'], values['size']))
        assert conn.execute('SELECT file_status FROM collection_file_writes').fetchone()[0] == 'pending'
        with conn:
            writes.complete(conn, identity, 'radio-session', 'episode',
                            (f'/files/by-id/{identity % 256:02x}/{identity}.json', 'a' * 64, 12))
        assert conn.execute('SELECT file_status FROM collection_file_writes').fetchone()[0] == 'ready'


@pytest.mark.parametrize('filename', ['/files/by-id/02/2.json', '/files/by-id/00/1.json',
                                      '/files/aa/' + 'a' * 64 + '.json'])
def test_completion_rejects_crossed_or_noncanonical_file_receipts(filename):
    with closing(sqlite3.connect(':memory:')) as conn:
        with conn:
            identity = writes.reserve(conn, 'radio-session', 'episode', ('a' * 64, 12))
            other = writes.reserve(conn, 'radio-session', 'episode', ('a' * 64, 12))
        assert (identity, other) == (1, 2)
        with pytest.raises(ValueError, match='reservation changed'), conn:
            writes.complete(conn, identity, 'radio-session', 'episode', (filename, 'a' * 64, 12))
        assert conn.execute('SELECT file_status FROM collection_file_writes ORDER BY id').fetchall() == [
            ('pending',), ('pending',)]
