"""Catalog display follows canonical identities without rewriting source labels."""
import sqlite3

import pytest

from yt_playlist.core import metadata


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Catalog SQL checks do not construct an HTTP application."""


def test_current_provider_choice_and_reset_follow_label_without_changing_evidence(store):
    store.genres.configure_catalog()
    entry = store.tracks.upsert_track('v', 'Song', 'Artist', None, None)
    with store.conn:
        metadata.select(store.conn, entry, 'genre', 'Drum And Bass', 'provider', now=10)
    assert store.conn.execute('SELECT genre FROM tracks WHERE id=?', (entry,)).fetchone()[0] == 'Drum & Bass'
    store.tracks.set_track_genre(entry, 'Jazz')
    with store.conn:
        store.conn.execute("UPDATE genre_catalog SET name='Jazz renamed' WHERE key='jazz'")
    assert store.conn.execute('SELECT genre FROM tracks WHERE id=?', (entry,)).fetchone()[0] == 'Jazz renamed'
    from yt_playlist.core.catalog import withdraw_choice
    with store.conn:
        withdraw_choice(store.conn, entry, 'genre', now=30)
    assert store.conn.execute('SELECT genre FROM tracks WHERE id=?', (entry,)).fetchone()[0] == 'Drum & Bass'
    assert [row[0] for row in store.conn.execute("SELECT text_value FROM metadata_observations "
        "WHERE field='genre' AND value_kind='value' ORDER BY id")] == ['Drum And Bass', 'Jazz']


def test_direct_sql_checks_and_explicit_metadata_selection(store):
    store.genres.configure_catalog()
    entry = store.tracks.upsert_track('v', 'Song', 'Artist', None, None)
    rid = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (entry,)).fetchone()[0]
    with store.conn:
        metadata.select(store.conn,entry,'genre','Jazz','fixture')
    assert store.conn.execute('SELECT genre_key,unresolved_genre FROM recordings WHERE id=?', (rid,)).fetchone()[:] == ('jazz', None)
    for sql in ("UPDATE recordings SET genre_key='absent'", "UPDATE recordings SET unresolved_genre='guess'",
                "DELETE FROM genre_catalog WHERE key='jazz'"):
        with pytest.raises(sqlite3.IntegrityError):
            with store.conn:
                store.conn.execute(sql)
