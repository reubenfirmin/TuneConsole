"""Current and installed frozen captures reject appends, replacements and reordering."""
from contextlib import closing, contextmanager
import sqlite3

import pytest

from yt_playlist.core import albums, album_tracklists, schema_inspection
from yt_playlist.core.migrations import current, migrate, registry, regular, Migration, MigrationError
from yt_playlist.core.migrations import runner
from yt_playlist.core.store import Store
from tests.historical_support import upgrade_fixture
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import install_numbered_input, native_sql_calls, without_query_timing


pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')


def capture(conn, *, legacy=False):
    with conn:
        aid = albums.observe(conn,'release',title='Release',artist='Artist',rank=1)
        snapshot = album_tracklists.freeze(conn,aid,'release','Frozen',['one','missing','one'],now=10,legacy=legacy)
        empty = album_tracklists.freeze(conn,aid,'release','Empty',[],now=11)
    return aid,snapshot,empty


def assert_frozen(conn, snapshot):
    before = list(map(tuple,conn.execute('SELECT * FROM album_share_items ORDER BY snapshot_id,position')))
    for operation in ('append','replace','update','delete'):
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                if operation in ('append','replace'):
                    conn.execute('INSERT OR REPLACE INTO album_share_items '
                        'SELECT snapshot_id,?,tracklist_id,occurrence_id,entry_id,raw_source_ref '
                        'FROM album_share_items WHERE snapshot_id=? AND position=0',
                        (3 if operation=='append' else 0,snapshot))
                elif operation=='update':
                    conn.execute("UPDATE album_share_items SET raw_source_ref='changed' WHERE snapshot_id=?",(snapshot,))
                else:
                    conn.execute('DELETE FROM album_share_items WHERE snapshot_id=?',(snapshot,))
        assert list(map(tuple,conn.execute('SELECT * FROM album_share_items ORDER BY snapshot_id,position')))==before
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def test_fresh_share_constructor_and_rollback_leave_no_mutable_prefix(store):
    _,snapshot,_ = capture(store.conn)
    assert_frozen(store.conn,snapshot)
    before = store.conn.execute('SELECT count(*) FROM album_share_snapshots').fetchone()[0]
    with pytest.raises(RuntimeError):
        with store.conn:
            album_tracklists.freeze(store.conn,albums.lookup(store.conn,'release'),'release','Rollback',['a','a'],now=12)
            raise RuntimeError('abort capture')
    assert store.conn.execute('SELECT count(*) FROM album_share_snapshots').fetchone()[0]==before
