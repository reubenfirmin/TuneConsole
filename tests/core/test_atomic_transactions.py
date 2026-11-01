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
