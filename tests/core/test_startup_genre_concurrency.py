"""Home and listening inputs keep reading while sync owns the serving connection."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from tests.core.test_predecessor_writes import installed
from yt_playlist.core import genre_identity
from yt_playlist.rec import home_themes, transient
from yt_playlist.util.matching import identity_key


@pytest.mark.parametrize('installed', [93, 107], indirect=True)
@pytest.mark.parametrize('consumer', ['facets', 'home_filter', 'home_options'])
def test_startup_genres_read_committed_identity_while_writer_is_busy(installed, monkeypatch, consumer):
    store, _ = installed
    entry = store.tracks.upsert_track('genre-concurrency', 'Owned genre', 'Artist', None, 200)
    store.tracks.set_track_genre(entry, 'Synth Pop')
    store.genres.rename_genre('synth pop', 'Renamed Synth')
    store.genres.set_genre_family('synth pop', 'jazz')
    key = identity_key('Owned genre', 'Artist')
    track = {'key': key, 'video_id': 'genre-concurrency', 'genre': 'Renamed Synth'}
    original = genre_identity.resolve
    reads = []

    def owned_read(conn, label):
        assert conn is not store.conn or store._lock.held_by_current_thread, 'Unowned serving connection'
        reads.append(conn)
        return original(conn, label)

    monkeypatch.setattr(genre_identity, 'resolve', owned_read)
    locked, release = threading.Event(), threading.Event()

    def sync():
        with store._lock, store.conn:
            store.conn.execute("INSERT INTO settings VALUES ('concurrent-sync','pending')")
            locked.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(sync)
        assert locked.wait(2)
        try:
            if consumer == 'facets':
                facets = transient.facets_for(store, [key])
                assert facets['genre:jazz'] == [key]
                assert facets['genre:synth pop'] == [key]
            elif consumer == 'home_filter':
                assert home_themes.matching_genres(store, [track], include=['jazz']) == [track]
                assert home_themes.matching_genres(store, [track], avoid=['jazz']) == []
            else:
                options = home_themes.genre_options(store, bundles={'all': {'wheelhouse': [track]}},
                                                    genre_plays={'Renamed Synth': 3})
                assert options == [{'family': 'jazz', 'label': 'renamed synth'}]
        finally:
            release.set()
        writer.result(2)
    assert reads and all(conn is not store.conn for conn in reads)
    assert store.settings.get_setting('concurrent-sync') == 'pending'


@pytest.mark.parametrize('installed', [93, 107], indirect=True)
def test_startup_genres_reuse_an_owned_uncommitted_transaction(installed):
    store, _ = installed
    entry = store.tracks.upsert_track('owned-genre', 'Owned genre', 'Artist', None, 200)
    store.tracks.set_track_genre(entry, 'Synth Pop')
    key = identity_key('Owned genre', 'Artist')
    with store._lock:
        store.conn.execute('BEGIN IMMEDIATE')
        try:
            store.conn.execute("UPDATE genre_catalog SET family_key='jazz' WHERE key='synth pop'")
            assert transient.facets_for(store, [key])['genre:jazz'] == [key]
            assert home_themes.matching_genres(store, [{'key': key}], include=['jazz'])
            assert home_themes.genre_options(store, bundles={'all': {'wheelhouse': [{'key': key}]}},
                                            genre_plays={}) == [{'family': 'jazz', 'label': 'synth-pop'}]
            assert home_themes.genre_options(store, genre_plays={}) == [{'family': 'jazz', 'label': 'synth-pop'}]
            assert store.conn.in_transaction
        finally:
            store.conn.rollback()
    assert 'genre:jazz' not in transient.facets_for(store, [key])
