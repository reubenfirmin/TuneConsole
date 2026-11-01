"""The selected entry credit has one owner after the physical rename."""
import sqlite3

import pytest

from yt_playlist.core.migrations import migrate, registry
from yt_playlist.core.store import Store


def test_rename_preserves_order_lineage_and_invalidation(tmp_path):
    store = Store(tmp_path / 'entries.db')
    try:
        migrate(store.conn, registry=registry.MIGRATIONS[:78])
        store._bind_repositories()
        entry = store.tracks.upsert_track('rename-video', 'Song', 'First, Second', None, 200,
            credits=[{'name': 'First', 'id': 'first'}, {'name': 'Second', 'id': 'second'}])
        rows = list(map(tuple, store.conn.execute('SELECT * FROM track_artists ORDER BY position')))
        selected = list(map(tuple, store.conn.execute('SELECT * FROM entry_artist_selections')))
        changes = store.conn.execute('SELECT evidence_changes FROM rec_preparation WHERE id=1').fetchone()[0]
        migrate(store.conn, registry=registry.MIGRATIONS)
        store.init_schema()
        assert list(map(tuple, store.conn.execute('SELECT * FROM entry_artists ORDER BY position'))) == rows
        assert list(map(tuple, store.conn.execute('SELECT * FROM entry_artist_selections'))) == selected
        assert store.conn.execute('SELECT evidence_changes FROM rec_preparation WHERE id=1').fetchone()[0] == changes
        assert not store.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='track_artists'").fetchone()
        assert not store.conn.execute("SELECT 1 FROM sqlite_schema WHERE sql LIKE '%track_artists%'").fetchone()
        assert [r['credit'] for r in store.artists.track_credits(entry)] == ['First', 'Second']
        with store.conn:
            store.conn.execute("UPDATE entry_artists SET credit='New credit' WHERE entry_id=? AND position=0", (entry,))
        assert store.conn.execute('SELECT evidence_changes FROM rec_preparation WHERE id=1').fetchone()[0] > changes
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        store.conn.close()


def test_fresh_entry_projection_is_used_by_ordinary_ingestion(tmp_path):
    store = Store(tmp_path / 'fresh.db')
    try:
        store.init_schema()
        entry = store.tracks.upsert_track('fresh-video', 'Song', 'First', None, 200,
                                         credits=[{'name': 'First', 'browseId': 'first'}])
        assert store.conn.execute('SELECT credit FROM entry_artists WHERE entry_id=?', (entry,)).fetchone()[0] == 'First'
        assert not store.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='track_artists'").fetchone()
        with pytest.raises(sqlite3.OperationalError, match='no such table: track_artists'):
            store.conn.execute('INSERT INTO track_artists SELECT * FROM entry_artists')
    finally:
        store.conn.close()
