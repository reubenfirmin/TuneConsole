"""Task 7 (#53): the Tools > Discovery Pools page lists the pools and the add-to-collection (Like)
action likes a discovered track."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from fastapi.testclient import TestClient

from yt_playlist.library.ops import PlaylistOps
from yt_playlist.rec import measurement
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    fake = FakeClient()
    return fake, TestClient(create_app(store, lambda: {iid: fake}, now_fn=lambda: 1000.0),
                            base_url="http://127.0.0.1")


def test_discovery_page_lists_pooled_track(store):
    store.discovery.upsert_discovered_track("a|x", "v1", "Cold Song", "Artist", "Alb", None, "house", "2020",
                                  "r", 100.0)
    store.discovery.mark_offered("track", ["a|x"], 200.0)
    _, c = _client(store)
    r = c.get("/discovery")
    assert r.status_code == 200
    assert "Cold Song" in r.text


def test_discovery_add_likes_track(store, monkeypatch):
    store.discovery.upsert_discovered_track("a|x", "v1", "Cold Song", "Artist", "Alb", None, None, None, "r", 100.0)
    fake, c = _client(store)
    owner = store.identities.get_master_identity().id
    lm = store.playlists.upsert_playlist(owner, 'LM', 'Liked Music', 0, '', 1)
    refreshed, saves = [], []
    monkeypatch.setattr(c.app.state.ctx.rec_worker, 'trigger_rating_update', lambda: refreshed.append(True))
    monkeypatch.setattr(measurement, 'record_action', lambda *args, **kwargs: saves.append(kwargs['keys']))
    r = c.post("/discovery/add", data={"kind": "track", "id": "a|x"})
    assert r.status_code == 200
    assert ("v1", "LIKE") in fake.rated      # rated thumbs-up -> Liked Music
    assert store.tracks.item_rating(owner, 'v1') == {
        'status': 'LIKE', 'source': 'api', 'observed_at': 1000}
    assert lm in store.playlists.playlist_ids_for_video('v1')
    assert store.rec_model.like_provenance('a|x') == 'action'
    assert refreshed == [True] and saves == [['a|x']]


@pytest.mark.parametrize('outcome', ['rejected', 'exception'])
def test_discovery_add_failed_rating_keeps_row_and_collection_unchanged(store, monkeypatch, outcome):
    store.discovery.upsert_discovered_track('a|x', 'v1', 'Cold Song', 'Artist', 'Alb', None,
                                           None, None, 'r', 100)
    fake, client = _client(store)
    owner = store.identities.get_master_identity().id
    lm = store.playlists.upsert_playlist(owner, 'LM', 'Liked Music', 0, '', 1)
    saves, refreshed = [], []
    monkeypatch.setattr(measurement, 'record_action', lambda *args, **kwargs: saves.append(True))
    monkeypatch.setattr(client.app.state.ctx.rec_worker, 'trigger_rating_update', lambda: refreshed.append(True))
    monkeypatch.setattr('yt_playlist.library.ratings.with_retry', lambda fn: fn())

    def rate(*args):
        if outcome == 'exception':
            raise OSError('offline')
        return {'status': 'STATUS_FAILED'}

    monkeypatch.setattr(fake, 'rate_song', rate)
    response = client.post('/discovery/add', data={'kind': 'track', 'id': 'a|x'})
    assert response.status_code == 502 and response.text
    assert store.tracks.item_rating(owner, 'v1') is None
    assert store.conn.execute("SELECT 1 FROM tracks WHERE video_id='v1'").fetchone() is None
    assert lm not in store.playlists.playlist_ids_for_video('v1')
    assert store.rec_model.recent_liked_keys() == []
    assert saves == [] and refreshed == []
    assert 'Cold Song' in client.get('/discovery').text


@pytest.mark.parametrize('phase', ['remote', 'publication'])
def test_discovery_save_and_ordinary_rating_share_the_whole_ordered_turn(store, monkeypatch, phase):
    store.discovery.upsert_discovered_track('a|x', 'v1', 'Cold Song', 'Artist', 'Alb', None,
                                           None, None, 'r', 100)
    fake, client = _client(store)
    owner = store.identities.get_master_identity().id
    lm = store.playlists.upsert_playlist(owner, 'LM', 'Liked Music', 0, '', 1)
    entered, release, queued, neutral_sent = (threading.Event() for _ in range(4))
    calls = []

    def pause():
        entered.set()
        assert release.wait(5)

    def rate(video, status):
        calls.append((video, status))
        if status == 'LIKE' and phase == 'remote':
            pause()
        if status == 'INDIFFERENT':
            neutral_sent.set()
        return {'status': 'STATUS_SUCCEEDED'}

    def record_action(*args, **kwargs):
        if phase == 'publication':
            pause()

    monkeypatch.setattr(fake, 'rate_song', rate)
    monkeypatch.setattr(measurement, 'record_action', record_action)

    def unlike():
        queued.set()
        return PlaylistOps(store, lambda: {owner: fake}, lambda: 1000).set_rating('v1', 'INDIFFERENT')

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.post, '/discovery/add', data={'kind': 'track', 'id': 'a|x'})
        try:
            assert entered.wait(5)
            last = pool.submit(unlike)
            assert queued.wait(5)
            assert not neutral_sent.wait(.1), 'Ordinary rating overtook an unfinished Discovery save'
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200
        last.result(timeout=5)
    assert calls == [('v1', 'LIKE'), ('v1', 'INDIFFERENT')]
    assert store.tracks.item_rating(owner, 'v1')['status'] == 'INDIFFERENT'
    assert lm not in store.playlists.playlist_ids_for_video('v1')
    assert store.rec_model.recent_liked_keys() == []


def test_discovery_add_without_video_keeps_pending_row(store):
    store.discovery.upsert_discovered_track('pending', None, 'Pending Song', 'Artist', None, None,
                                           None, None, None, 100)
    fake, client = _client(store)
    response = client.post('/discovery/add', data={'kind': 'track', 'id': 'pending'})
    assert response.status_code == 422 and response.text
    assert fake.rated == []
    assert 'Pending Song' in client.get('/discovery').text


def test_discovery_add_failed_acquisition_retains_confirmed_rating_and_can_retry(store, monkeypatch):
    store.discovery.upsert_discovered_track('a|x', 'v1', 'Cold Song', 'Artist', 'Alb', None,
                                           None, None, 'r', 100)
    _, client = _client(store)
    owner = store.identities.get_master_identity().id
    saves = []
    monkeypatch.setattr(measurement, 'record_action', lambda *args, **kwargs: saves.append(True))

    def fail(*args, **kwargs):
        raise RuntimeError('acquisition failed')

    with monkeypatch.context() as patch:
        patch.setattr(store.tracks, 'upsert_track', fail)
        response = client.post('/discovery/add', data={'kind': 'track', 'id': 'a|x'})
    assert response.status_code == 502 and 'YouTube saved the like' in response.text
    assert store.tracks.item_rating(owner, 'v1')['status'] == 'LIKE'
    assert store.conn.execute("SELECT 1 FROM tracks WHERE video_id='v1'").fetchone() is None
    assert saves == []
    assert client.post('/discovery/add', data={'kind': 'track', 'id': 'a|x'}).status_code == 200
    assert saves == [True]


def test_fragment_loads_do_not_count_as_album_or_artist_views(store):
    store.discovery.upsert_discovered_album("album", "Artist", "Unseen album", "2026", None, 100.0)
    store.discovery.upsert_discovered_artist("New artist", 1.0, [], [], None, 100.0)
    _, c = _client(store)
    for _ in range(3):
        assert c.get("/home/discover").status_code == 200
        assert c.get("/home/new-artists").status_code == 200
    assert store.discovery.get_discovered_albums()[0]["offered_count"] == 0
    assert store.discovery.get_discovered_artists()[0]["offered_count"] == 0
    assert "less than 1d ago" in c.get("/discovery").text


def test_visible_tile_endpoint_counts_once_and_validates_kind(store):
    from uuid import uuid4

    store.discovery.upsert_discovered_album("album", "Artist", "Album", "2026", None, 100.0)
    _, c = _client(store)
    payload = {"kind": "album", "ids": ["album"], "view_id": str(uuid4())}
    assert c.post("/discovery/impressions", json=payload).status_code == 204
    assert c.post("/discovery/impressions", json=payload).status_code == 204
    assert store.discovery.get_discovered_albums()[0]["offered_count"] == 1
    payload["kind"] = "anything"
    assert c.post("/discovery/impressions", json=payload).status_code == 422


def test_saved_metadata_is_visible_without_queue_or_provider_details(store):
    store.discovery.upsert_discovered_track("cold", "v", "Cold Song", "Artist", "Album", None,
                                  "House", "2020", "album", 100,
                                  audio={"bpm": 128.0, "energy": .8, "danceability": .7})
    store.enrichment_state.record_enrichment_attempt("cold", "musicbrainz", {"year": "2020"}, now=200)
    store.enrichment_state.record_enrichment_attempt("cold", "deezer", {}, now=201)
    store.enrichment_state.record_enrichment_attempt("cold", "acousticbrainz", {}, retry=True, now=202)
    _, client = _client(store)
    page = client.get("/discovery").text
    for text in ("House", "2020", "128.0 BPM", "Energy 0.8", "Danceability 0.7"):
        assert text in page
    assert "Queued for enrichment" not in page and "Provider details" not in page


def test_track_charts_count_saved_fields_and_ignore_completion_stamps():
    from yt_playlist.web.routes.discovery import track_pool_summary

    summary = track_pool_summary([
        {"genre": "House", "year": "1999", "audio": {"energy": 0}, "last_enriched": None},
        {"genre": "house", "year": "1991", "audio": {}, "last_enriched": 100},
        {"genre": None, "year": "invalid", "audio": {}, "last_enriched": 100},
        {"genre": "Jazz", "year": "2026", "audio": {}, "last_enriched": None},
    ])
    assert [(r["label"], r["count"]) for r in summary["genres"]] == [("House", 2), ("Jazz", 1), ("No genre", 1)]
    assert [(r["label"], r["count"]) for r in summary["eras"]] == [("1990s", 2), ("2020s", 1), ("No year", 1)]
    assert [(r["label"], r["count"]) for r in summary["gaps"]] == [("No genre", 1), ("No year", 1), ("No audio features", 3)]
    assert summary["without_metadata"] == 1
    assert summary["genres"][0]["pct"] == 50
