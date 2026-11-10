"""Repository completion cannot take ownership of a joined transaction."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.core.db_diagnostics import DiagnosticConnection


@pytest.mark.parametrize('failure', ['exception', 'rollback', 'context', 'nested'])
def test_joined_failure_rolls_back_only_its_unit(tmp_path, failure):
    with closing(sqlite3.connect(tmp_path / 'units.db', factory=DiagnosticConnection)) as conn:
        conn.execute('CREATE TABLE drafts (value TEXT)')
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("INSERT INTO drafts VALUES ('caller')")
        with pytest.raises((ValueError, RuntimeError)):
            with conn.atomic(join=True):
                conn.execute("INSERT INTO drafts VALUES ('unit')")
                conn.commit()
                if failure == 'rollback':
                    conn.rollback()
                elif failure == 'context':
                    try:
                        with conn:
                            raise ValueError('repository failed')
                    except ValueError:
                        pass
                elif failure == 'nested':
                    try:
                        with conn.atomic(join=True):
                            raise ValueError('nested unit failed')
                    except ValueError:
                        pass
                else:
                    raise ValueError('unit failed')
        assert conn.in_transaction
        assert conn.execute('SELECT value FROM drafts').fetchall() == [('caller',)]
        conn.commit()
        with closing(sqlite3.connect(tmp_path / 'units.db')) as observer:
            assert observer.execute('SELECT value FROM drafts').fetchall() == [('caller',)]


@pytest.mark.parametrize('borrowed', [False, True])
def test_atomic_completion_respects_its_owner(tmp_path, borrowed):
    path = tmp_path / 'units.db'
    with closing(sqlite3.connect(path, factory=DiagnosticConnection)) as conn:
        conn.execute('CREATE TABLE drafts (value TEXT)')
        if borrowed:
            conn.execute('BEGIN IMMEDIATE')
        with conn.atomic(join=True):
            conn.execute("INSERT INTO drafts VALUES ('unit')")
            with conn:
                conn.execute("INSERT INTO drafts VALUES ('repository context')")
            with conn.atomic(join=True):
                conn.commit()
        assert conn.in_transaction == borrowed
        with closing(sqlite3.connect(path)) as observer:
            assert observer.execute('SELECT count(*) FROM drafts').fetchone()[0] == (0 if borrowed else 2)
        conn.commit()
        with closing(sqlite3.connect(path)) as observer:
            assert observer.execute('SELECT count(*) FROM drafts').fetchone()[0] == 2


def test_default_atomic_still_requires_idle_connection(tmp_path):
    with closing(sqlite3.connect(tmp_path / 'units.db', factory=DiagnosticConnection)) as conn:
        conn.execute('BEGIN')
        with pytest.raises(RuntimeError, match='idle connection'):
            with conn.atomic():
                pytest.fail('Admitted an unrequested join')
        assert conn.in_transaction
        conn.rollback()
        with conn.atomic():
            with pytest.raises(RuntimeError, match='idle connection'):
                with conn.atomic():
                    pytest.fail('Admitted an unrequested nested unit')


@pytest.mark.parametrize('borrowed', [False, True])
@pytest.mark.parametrize('write', [False, True])
def test_sqlite_interruption_preserves_reason_and_transaction_ownership(tmp_path, borrowed, write):
    path = tmp_path / 'interrupted.db'
    with closing(sqlite3.connect(path, factory=DiagnosticConnection)) as conn:
        conn.execute('CREATE TABLE drafts (value INTEGER)')
        conn.execute('INSERT INTO drafts VALUES (100)')
        conn.commit()
        if borrowed:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT INTO drafts VALUES (200)')
        cancelled = False
        conn.set_progress_handler(lambda: int(cancelled), 1000)
        try:
            with pytest.raises(sqlite3.OperationalError, match='^interrupted$'):
                with conn.atomic(join=True):
                    conn.execute('INSERT INTO drafts VALUES (300)')
                    cancelled = True
                    query = ('WITH RECURSIVE n(value) AS (SELECT 1 UNION ALL '
                             'SELECT value+1 FROM n WHERE value<10000) ')
                    query += ('INSERT INTO drafts SELECT value FROM n' if write
                              else 'SELECT sum(value) FROM n')
                    conn.execute(query).fetchall()
        finally:
            conn.set_progress_handler(None, 0)
        # Interrupting a write aborts SQLite's entire transaction. A failed read
        # permits local rollback while the caller retains its earlier write.
        retained_outer = borrowed and not write
        assert conn.in_transaction == retained_outer
        expected = [(100,), (200,)] if retained_outer else [(100,)]
        assert conn.execute('SELECT value FROM drafts').fetchall() == expected
        conn.commit()
    with closing(sqlite3.connect(path)) as observer:
        assert observer.execute('SELECT value FROM drafts').fetchall() == expected


def test_joined_cleanup_still_reports_a_missing_savepoint_in_an_active_transaction(tmp_path):
    with closing(sqlite3.connect(tmp_path / 'savepoint.db', factory=DiagnosticConnection)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError, match='no such savepoint'):
            with conn.atomic(join=True):
                conn.execute('RELEASE SAVEPOINT tuneconsole_atomic_unit')
                raise ValueError('unit failed after losing its savepoint')
        assert conn.in_transaction
        conn.rollback()
