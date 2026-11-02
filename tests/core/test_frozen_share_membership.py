"""Current and installed frozen captures reject appends, replacements and reordering."""
import sqlite3

import pytest

from yt_playlist.core import albums, album_tracklists, genre_identity
from yt_playlist.core.migrations import current, migrate, registry, Migration, MigrationError
from yt_playlist.core.migrations import v105_frozen_share_membership as membership
from yt_playlist.core.store import Store
from tests.historical_support import upgrade_fixture


def installed(path):
    store = Store(path)
    for sql in current.statements(version=104):
        store.conn.execute(sql)
    genre_identity.seed_catalog(store.conn)
    store.conn.executemany('INSERT INTO settings VALUES (?,?)',current.SEED_SETTINGS.items())
    for sql in ('INSERT INTO rec_preparation VALUES (1,0,0)', 'INSERT INTO rec_group_clock VALUES (1,0)',
                'INSERT INTO group_retention_state VALUES (1,0)'):
        store.conn.execute(sql)
    store.conn.executemany('INSERT INTO schema_migrations VALUES (?,?,1)',
                          [(m.version,m.name) for m in registry.MIGRATIONS[:104]])
    store.conn.commit()
    return store


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


@pytest.mark.parametrize('legacy',[False,True])
def test_installed_shares_keep_exact_membership_across_upgrade_alias_and_restart(tmp_path,legacy):
    path = tmp_path/'shares.db'
    store = installed(path)
    aid,snapshot,empty = capture(store.conn,legacy=legacy)
    before = list(map(tuple,store.conn.execute('SELECT * FROM album_share_items ORDER BY snapshot_id,position')))
    upgrade_fixture(store)
    assert list(map(tuple,store.conn.execute('SELECT * FROM album_share_items ORDER BY snapshot_id,position')))==before
    assert_frozen(store.conn,snapshot)
    with store.conn:
        target = albums.observe(store.conn,'alias',title='Alias',artist='Artist',rank=1)
        # Provider evidence moves with its verified release owner before capture ownership.
        store.conn.execute('UPDATE album_provider_ids SET is_primary=0,equivalence_digest=?,verified_at=10 WHERE album_id=?',('verified',aid))
        store.conn.execute('UPDATE album_provider_ids SET album_id=? WHERE album_id=?',(target,aid))
        album_tracklists.merge(store.conn,target,aid)
    assert_frozen(store.conn,snapshot)
    assert store.conn.execute('SELECT count(*) FROM album_share_items WHERE snapshot_id=?',(empty,)).fetchone()[0]==0
    current.validate(store.conn)
    store.conn.close()
    reopened = Store(path)
    try:
        reopened.init_schema()
        assert_frozen(reopened.conn,snapshot)
        assert list(map(tuple,reopened.conn.execute('SELECT * FROM album_share_items ORDER BY snapshot_id,position')))==before
    finally:
        reopened.conn.close()


def test_fresh_share_constructor_and_rollback_leave_no_mutable_prefix(store):
    _,snapshot,_ = capture(store.conn)
    assert_frozen(store.conn,snapshot)
    before = store.conn.execute('SELECT count(*) FROM album_share_snapshots').fetchone()[0]
    with pytest.raises(RuntimeError):
        with store.conn:
            album_tracklists.freeze(store.conn,albums.lookup(store.conn,'release'),'release','Rollback',['a','a'],now=12)
            raise RuntimeError('abort capture')
    assert store.conn.execute('SELECT count(*) FROM album_share_snapshots').fetchone()[0]==before


def test_interrupted_guard_install_rolls_back_then_restarts(tmp_path):
    store = installed(tmp_path/'restart.db')
    _,snapshot,_ = capture(store.conn)
    before = list(store.conn.iterdump())
    def interrupted(conn,context):
        membership.apply(conn,context)
        raise RuntimeError('interrupted share guard')
    with pytest.raises(MigrationError,match='interrupted share guard'):
        migrate(store.conn,registry=(*registry.MIGRATIONS[:104],Migration(105,'frozen_share_membership',interrupted)))
    assert list(store.conn.iterdump())==before
    upgrade_fixture(store)
    assert_frozen(store.conn,snapshot)
    store.conn.close()


def test_preexisting_appended_share_stops_without_changing_original_rows(tmp_path):
    store = installed(tmp_path/'invalid.db')
    _,snapshot,_ = capture(store.conn)
    with store.conn:
        store.conn.execute('INSERT INTO album_share_items SELECT snapshot_id,3,tracklist_id,occurrence_id,entry_id,raw_source_ref '
                           'FROM album_share_items WHERE snapshot_id=? AND position=0',(snapshot,))
    before = list(store.conn.iterdump())
    with pytest.raises(MigrationError,match='changed membership; original rows retained'):
        migrate(store.conn, registry=registry.MIGRATIONS)
    assert list(store.conn.iterdump())==before
    store.conn.close()
