"""Genuine v73 inputs for readers which compare legacy and typed storage."""
from contextlib import closing
import sqlite3

import pytest

from tests.core.test_compact_groups_migration import apply as compact_groups, historical_groups
from tests.genre_catalog_support import bound_catalog
from yt_playlist.core import schema_inspection
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.migrations import registry, runner
from yt_playlist.core.migrations import v073_recommendation_publications as migration
from yt_playlist.core.migrations.runner import Context
from yt_playlist.core.store import Store


@pytest.fixture(scope='module')
def recommendation_predecessor():
    # Real v70 conversion reads retired v69 tables. Modern assembled SQL is
    # insufficient for these compatibility inputs, even when its shape agrees.
    with pytest.MonkeyPatch.context() as patch, closing(sqlite3.connect(':memory:')) as source:
        bound_catalog(patch, entries=(('house', 'House'),))
        source.execute('PRAGMA foreign_keys=ON')
        runner.migrate(source, registry=registry.MIGRATIONS[:69])
        source.execute('PRAGMA query_only=ON')
        yield source


@pytest.fixture
def recommendations(tmp_path, recommendation_predecessor):
    assert recommendation_predecessor.execute('PRAGMA query_only').fetchone()[0] == 1
    path = tmp_path / 'recommendations.db'
    with historical_groups(path, recommendation_predecessor) as conn:
        compact_groups(conn)
    store = Store(path)
    with closing(store.conn), schema_inspection.reuse_tables(store.conn):
        store.conn.execute('PRAGMA synchronous=OFF')
        assert store.conn.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        store.conn.execute('PRAGMA wal_autocheckpoint=0')
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            migration.apply(store.conn, Context(4))
        migration.validate(store.conn)
        # Individually applied real boundaries do not form a contiguous startup
        # history. Bind the same legacy name projections as installed readers.
        store._schema_compatibility = True
        store._bind_repositories()
        store._snapshot_revisions = Revisions(store.conn)
        assert store.taste_groups.head_generation() == 'second'
        assert store.taste_groups.head(members=True)['groups'][1]['members'] == {'one': (.9, .8)}
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
        yield store
