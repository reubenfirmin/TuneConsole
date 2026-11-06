"""Repository reads and album policy cannot consume another writer's draft."""
from concurrent.futures import ThreadPoolExecutor

from yt_playlist.core import discovery_checkpoints
from yt_playlist.rec import album_choices


def test_album_selection_and_fetch_history_use_committed_data(file_backed_store):
    store = file_backed_store
    album = {'browse_id': 'owned-album', 'artist': 'Artist', 'genre': 'Synth Pop',
             'classification': 'discovery'}
    store.discovery.upsert_discovered_album('owned-album', 'Artist', 'Album', None, None, 1)

    def select():
        _, chosen, _ = album_choices.selection(store, [album], 10, {'novelty': .5}, genres=['pop'])
        return [item['browse_id'] for item in chosen], store.discovery.fetched_albums(['owned-album'])

    assert select() == (['owned-album'], set())
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store._lock:
            store.conn.execute('BEGIN IMMEDIATE')
            try:
                store.conn.execute("UPDATE genre_catalog SET family_key='jazz' WHERE key='synth pop'")
                discovery_checkpoints.remember(store.conn, 'owned-album')
                assert pool.submit(select).result(timeout=5) == (['owned-album'], set())
                assert store.conn.in_transaction
            finally:
                store.conn.rollback()
    assert select() == (['owned-album'], set())
    store.discovery.remember_album_fetch('owned-album')
    assert store.discovery.fetched_albums(['owned-album', 'missing']) == {'owned-album'}
