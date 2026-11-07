"""Home and listening inputs keep reading while sync owns the serving connection."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from tests.core.test_predecessor_writes import installed
from yt_playlist.core import genre_identity
from yt_playlist.rec import home_themes, transient
from yt_playlist.repos.rec_query import RecQueryRepo
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
    original = genre_identity.axis_match_rows
    reads, metadata_reads, statements = [], [], []
    locked, release = threading.Event(), threading.Event()

    def owned_read(conn, label):
        assert conn is not store.conn or store._lock.held_by_current_thread, 'Unowned serving connection'
        assert locked.is_set() and not release.is_set() and not writer.done()
        reads.append(conn)
        conn.set_trace_callback(statements.append)
        try:
            result = original(conn, label)
        finally:
            conn.set_trace_callback(None)
        expected_label = 'renamed synth' if consumer == 'home_options' else 'Renamed Synth'
        assert genre_identity.axes_from_matches(result) == {expected_label: ('jazz', 'synth pop')}
        return result

    monkeypatch.setattr(genre_identity, 'axis_match_rows', owned_read)
    if consumer == 'facets':
        for method in ('track_genres', 'track_decades', 'track_artists'):
            metadata = getattr(RecQueryRepo, method)

            def owned_metadata(repo, keys, _metadata=metadata):
                assert repo.conn is not store.conn
                assert locked.is_set() and not release.is_set() and not writer.done()
                metadata_reads.append(repo.conn)
                return _metadata(repo, keys)

            monkeypatch.setattr(RecQueryRepo, method, owned_metadata)

    def sync():
        with store._lock, store.conn:
            store.conn.execute("INSERT INTO settings VALUES ('concurrent-sync','pending')")
            store.conn.execute("UPDATE genre_catalog SET family_key='pop' WHERE key='synth pop'")
            locked.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(sync)
        try:
            if not locked.wait(2):
                writer.result(1)
                pytest.fail('Concurrent writer did not acquire the serving connection')
            with pytest.raises(AssertionError, match='Unowned serving connection'):
                genre_identity.axis_matches(store.conn, ['Renamed Synth'])
            assert not reads
            if consumer == 'facets':
                facets = transient.facets_for(store, [key])
                assert facets['genre:jazz'] == [key]
                assert facets['genre:synth pop'] == [key]
                assert len(metadata_reads) == 3
                assert all(conn is reads[0] for conn in metadata_reads)
                assert any(sql.startswith('WITH wanted') and 'genre_catalog' in sql for sql in statements)
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
    assert any(sql.startswith('WITH wanted') and 'genre_catalog' in sql for sql in statements)
    assert store.settings.get_setting('concurrent-sync') == 'pending'
    with store._lock:
        assert store.conn.execute("SELECT family_key FROM genre_catalog WHERE key='synth pop'").fetchone()[0] == 'pop'


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
