"""Every supported post-release change rolls back atomically and can retry."""
from contextlib import closing
from dataclasses import replace
import sqlite3

import pytest

from tests.genre_catalog_support import bound_catalog
from tests.release_schema_support import release_schema
from yt_playlist.core.migrations import regular
from yt_playlist.core.migrations.runner import RELEASE_CHANGES


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """The runner checks do not construct an application."""


@pytest.fixture(scope='module')
def released():
    with closing(sqlite3.connect(':memory:')) as conn, pytest.MonkeyPatch.context() as patch:
        bound_catalog(patch)
        with conn:
            release_schema(conn)
        conn.execute('PRAGMA query_only=ON')
        yield conn


@pytest.mark.parametrize('change', regular.CHANGES[RELEASE_CHANGES:], ids=lambda item: item.name)
@pytest.mark.parametrize('boundary', ['apply', 'validate'])
def test_supported_change_rolls_back_and_retries(released, tmp_path, change, boundary):
    with closing(sqlite3.connect(tmp_path / 'retry.db')) as conn:
        released.backup(conn)
        conn.execute('PRAGMA foreign_keys=ON')
        regular.migrate(conn, changes=regular.CHANGES[:change.version - 1])
        before = list(conn.iterdump())

        def apply(target, context):
            change.apply(target, context)
            if boundary == 'apply':
                raise InterruptedError('interrupted bounded change')

        def validate(target):
            change.validate(target)
            if boundary == 'validate':
                raise InterruptedError('interrupted bounded change')

        interrupted = replace(change, apply=apply, validate=validate)
        prefix = (*regular.CHANGES[:change.version - 1], interrupted)
        with pytest.raises(InterruptedError, match='interrupted bounded change'):
            regular.migrate(conn, changes=prefix)
        assert not conn.in_transaction
        assert list(conn.iterdump()) == before
        regular.migrate(conn, changes=regular.CHANGES[:change.version])
        assert regular.validate(conn) == change.version
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        with conn:
            conn.execute("INSERT INTO settings VALUES ('after-retry','writable')")
        regular.migrate(conn, changes=regular.CHANGES[:change.version])
        assert conn.execute("SELECT value FROM settings WHERE key='after-retry'").fetchone() == ('writable',)
