"""Analytics release the serving connection and retain coherent publication inputs."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from yt_playlist.core.store import Store
from yt_playlist.util.matching import identity_key
from yt_playlist.web.app import create_app
from yt_playlist.web.routes import charts as chart_routes


@pytest.fixture
def library(tmp_path):
    store = Store(tmp_path / 'library.db')
    store.init_schema()
    account = store.identities.upsert_identity('main', 'cred', None, True)
    entry = store.tracks.upsert_track('video', 'Song', 'Artist', 'Album', 180,
                                     album_browse_id='B', thumbnail='/cover.png')
    playlist = store.playlists.upsert_playlist(account, 'PL', 'Mix', 1, 'h', 0)
    store.playlists.set_playlist_tracks(playlist, [entry])
    store.collection.add_saved_album({'browse': 'B', 'title': 'Album', 'artist': 'Artist'})
    store.history.record_play_event(account, identity_key('Song', 'Artist'), 'video', 100)
    yield store, account, playlist
    store.conn.close()


OPERATIONS = [
    ('charts', 'ticker_thumbnails', ('artist',)),
    ('charts', 'history_bounds', ()),
    ('charts', 'corpus_distribution', ('artist',)),
    ('charts', 'listen_distribution', ('artist',)),
    ('charts', 'get_playlist_listen_stats', ()),
    ('charts', 'top_tracks', ()),
    ('charts', 'playlist_tracks_detail', ('playlist',)),
    ('charts', 'album_tracks_detail', ('B',)),
    ('charts', 'artist_songs', ('Artist',)),
    ('collection', 'taste_album_contexts', ()),
    ('collection', 'saved_albums_recency', ()),
    ('collection', 'collection_albums', ()),
    ('collection', 'collection_artists', ()),
    ('collection', 'artist_albums', ('Artist',)),
    ('trends', 'repetition_series', (5,)),
    ('trends', 'ledger_day_counts', ()),
    ('trends', 'ledger_track_plays', (0, 200)),
    ('trends', 'ledger_artist_plays', (0, 200)),
    ('trends', 'play_hours', (0, 200)),
    ('trends', 'history_track_first', ()),
    ('trends', 'play_event_track_first', ()),
]


@pytest.mark.parametrize('repo_name,method,args', OPERATIONS,
                         ids=[f'{repo}.{method}' for repo, method, _ in OPERATIONS])
def test_slow_analytics_do_not_block_foreground_writes(library, monkeypatch, repo_name, method, args):
    store, _, playlist = library
    entered, release = threading.Event(), threading.Event()
    connections, timed_out = [], []
    original = store._make_reader

    def make_reader():
        reader = original()
        connections.append(reader.conn)

        def trace(sql):
            if (not entered.is_set() and sql.lstrip().upper().startswith(('SELECT', 'WITH'))
                    and 'sqlite_schema' not in sql):
                entered.set()
                if not release.wait(5):
                    timed_out.append(sql)

        reader.conn.set_trace_callback(trace)
        return reader

    monkeypatch.setattr(store, '_make_reader', make_reader)
    values = tuple(playlist if value == 'playlist' else value for value in args)
    with ThreadPoolExecutor(2) as workers:
        pending = workers.submit(getattr(getattr(store, repo_name), method), *values)
        try:
            assert entered.wait(5), 'Analytics never used a private reader'
            workers.submit(store.settings.set_setting, 'foreground', 'saved').result(2)
        finally:
            release.set()
        assert isinstance(pending.result(5), (dict, list, tuple))
    assert not timed_out
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connections[0].execute('SELECT 1')
    assert store.settings.get_setting('foreground') == 'saved'


def test_playlist_analytics_in_owned_transaction_see_uncommitted_membership(library, monkeypatch):
    store, _, playlist = library
    assert len(store.charts.playlist_tracks_detail(playlist)) == 1

    def forbidden():
        pytest.fail('Owned transaction opened a separate committed-data reader')

    monkeypatch.setattr(store, '_make_reader', forbidden)
    with store._lock:
        store.conn.execute('DELETE FROM playlist_tracks WHERE playlist_id=?', (playlist,))
        try:
            assert store.charts.playlist_tracks_detail(playlist) == []
        finally:
            store.conn.rollback()


def test_album_context_memo_keeps_history_dependencies(library):
    store, account, _ = library
    key = identity_key('Song', 'Artist')
    # Warm the real guarded-reader memo, then hit it in another snapshot.
    with store.read_snapshot() as first:
        assert first.store.collection.taste_album_contexts()
    with store.read_snapshot() as second:
        assert second.store.collection.taste_album_contexts()
        store.history.record_play_event(account, key, 'video', 2 * 86400)
        assert not second.publish(lambda: pytest.fail('Memoized album context lost its evidence dependency'))


def test_chart_dimensions_and_ranking_share_one_committed_view(library, monkeypatch):
    store, account, _ = library
    original, changed = chart_routes._build_ticker, []

    def ticker(reader, dimension, periods):
        result = original(reader, dimension, periods)
        if not changed:
            changed.append(True)
            store.tracks.upsert_track('late', 'Later', 'Later artist', None, 180)
            store.history.record_history_plays(account, 86400,
                [identity_key('Later', 'Later artist')], source_timezone='UTC')
        return result

    monkeypatch.setattr(chart_routes, '_build_ticker', ticker)
    app = create_app(store, lambda: {}, now_fn=lambda: 3 * 86400, background_work=False)
    client = TestClient(app, base_url='http://127.0.0.1')
    first = client.get('/charts?view=songs')
    assert first.status_code == 200
    assert [song['title'] for song in first.context['songs']] == ['Song']
    assert 'Later artist' not in {row['cat'] for row in first.context['tickers']['artist']['rows']}
    following = client.get('/charts?view=songs')
    assert following.status_code == 200
    assert {song['title'] for song in following.context['songs']} == {'Song', 'Later'}


def test_artist_saved_flags_and_owners_share_one_committed_view(library, monkeypatch):
    from yt_playlist.repos.charts import ChartsRepo
    store, _, _ = library
    original, changed = ChartsRepo.artist_songs, []

    def songs(reader, *args, **kwargs):
        result = original(reader, *args, **kwargs)
        if not changed:
            changed.append(True)
            store.collection.remove_saved_album('B')
        return result

    monkeypatch.setattr(ChartsRepo, 'artist_songs', songs)
    monkeypatch.setattr(chart_routes, 'fetch_artist_info', lambda *args: None)
    app = create_app(store, lambda: {}, now_fn=lambda: 3 * 86400, background_work=False)
    client = TestClient(app, base_url='http://127.0.0.1')
    first = client.get('/artist?name=Artist')
    assert first.status_code == 200
    assert first.context['collection'][0]['saved']
    assert 'B' in first.context['saved_owners']
    following = client.get('/artist?name=Artist')
    assert following.status_code == 200
    assert not following.context['collection'][0]['saved']
    assert 'B' not in following.context['saved_owners']
