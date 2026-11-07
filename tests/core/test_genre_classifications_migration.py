"""Catalog display follows canonical identities without rewriting source labels."""
import sqlite3

import pytest

from yt_playlist.core import albums, genre_classifications, metadata
from yt_playlist.core.migrations import current, migrate, registry
from yt_playlist.core.store import Store


@pytest.fixture
def predecessor(tmp_path, _pristine_installed_schema):
    store = Store(tmp_path / 'classification.db')
    _pristine_installed_schema(85).backup(store.conn)
    store._bind_repositories()
    store.genres.configure_catalog()
    yield store
    store.conn.close()


def test_registered_upgrade_retains_alias_unknown_null_and_blank(predecessor):
    conn = predecessor.conn
    labels = ['Drum And Bass', 'Synth Pop', 'Fix Artist Credits', '', None]
    with conn:
        for index, label in enumerate(labels):
            conn.execute('INSERT INTO recordings(id,identity_key,genre) VALUES (?,?,?)',
                         (index + 1, 'recording:' + str(index), label))
        aid = albums.observe(conn, 'release', title='Release', artist='Artist', genre='Drum And Bass')
    predecessor.discovery.upsert_discovered_artist('New artist', 1, [], [], None, 10, genre='Drum And Bass')
    migrate(conn, registry=registry.MIGRATIONS)
    predecessor._bind_repositories()
    assert [row[0] for row in conn.execute('SELECT genre_key FROM recordings ORDER BY id')] == [
        'drum bass', 'synth pop', None, None, None]
    assert [row[0] for row in conn.execute('SELECT unresolved_genre FROM recordings ORDER BY id')] == [
        None, None, 'Fix Artist Credits', '', None]
    assert [row[0] for row in conn.execute('SELECT raw_label FROM genre_classification_legacy '
        'WHERE recording_id IS NOT NULL ORDER BY recording_id')] == labels[:-1]
    assert genre_classifications.read(conn, 'albums', aid) == 'Drum & Bass'
    assert predecessor.discovery.get_discovered_artists()[0]['genre'] == 'Drum & Bass'
    with conn:
        conn.execute("UPDATE genre_catalog SET name='Drums renamed' WHERE key='drum bass'")
    assert genre_classifications.read(conn, 'recordings', 1) == 'Drums renamed'
    assert genre_classifications.read(conn, 'albums', aid) == 'Drums renamed'
    assert predecessor.discovery.get_discovered_artists()[0]['genre'] == 'Drums renamed'
    assert conn.execute('SELECT raw_label FROM genre_classification_legacy WHERE recording_id=1').fetchone()[0] == labels[0]
    current.validate(conn)
    reopened = Store(predecessor._db_path)
    try:
        reopened.init_schema()
        assert genre_classifications.read(reopened.conn, 'recordings', 1) == 'Drums renamed'
    finally:
        reopened.conn.close()


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


def test_fresh_and_registered_upgrade_have_same_contract(predecessor, store):
    migrate(predecessor.conn, registry=registry.MIGRATIONS)
    for table in ('recordings', 'albums', 'discovered_artists'):
        def shape(conn):
            sql = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (table,)).fetchone()[0]
            return current._table(conn, table, sql)
        assert shape(store.conn) == shape(predecessor.conn)
