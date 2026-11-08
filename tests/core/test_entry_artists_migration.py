"""The selected entry credit has one owner after the physical rename."""
import sqlite3

import pytest

from yt_playlist.core.store import Store


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
