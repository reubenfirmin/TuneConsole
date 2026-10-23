"""Home Discover serves prepared pools immediately and never fits models on a request."""
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from yt_playlist.rec import discover, fresh_pool, home_prepared, surfaces
from yt_playlist.web.app import create_app


def seed_songs(store, count=40):
    for i in range(count):
        store.discovery.upsert_discovered_track(f"song {i}|artist {i}", f"v{i}", f"Song {i}",
            f"Artist {i}", None, None, "Jazz", "2020", "radio:seed", 1)
    fresh_pool.prepare(store, 2)


def client_for(store, busy="idle"):
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    iid = store.identities.upsert_identity("main", "cred", None, True)
    assert home_prepared.prepare(store, 3) is not None
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 3)
    app.state.ctx.rec_worker._model_active = busy == "rebuild"
    app.state.ctx.rec_worker._discovering = busy == "discovery"
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.mark.parametrize("busy", ["idle", "rebuild", "discovery"])
def test_persisted_recommendations_are_in_initial_home_and_survive_refresh(store, monkeypatch, busy):
    seed_songs(store)
    store.discovery.upsert_discovered_album("album", "Artist", "Ready album", "2026", None, 1)
    store.discovery.upsert_discovered_artist("New artist", 1, [], [], None, 1)
    client = client_for(store, busy)
    assert all(row["offered_count"] == 0 for row in store.discovery.get_discovered_tracks())

    def forbidden(*args, **kwargs):
        raise AssertionError("Page reads must not discover, fit or rank candidates")

    monkeypatch.setattr(discover, "run_discovery", forbidden)
    monkeypatch.setattr(discover.ContentProjection, "fit", forbidden)
    monkeypatch.setattr(surfaces, "cold_candidates", forbidden)
    monkeypatch.setattr(fresh_pool, "prepare", forbidden)
    mix_offers = None
    for path in ("/", "/home/discovery", "/home/discovery"):
        response = client.get(path)
        assert response.status_code == 200
        for text in ("Ready album", "New artist", "Songs to discover", "Song 0"):
            assert text in response.text
        fragment = re.search(r'<div id="home-discovery".*', response.text, re.S)[0]
        assert fragment.count('hx-get="/home/discovery"') == int(busy != "idle")
        assert "Finding new music for you" not in fragment
        assert 'tile-badge' not in fragment
        assert 'Updating your recommendations' not in fragment
        assert 'fitting the playlists' not in fragment
        counts = {row["identity_key"]: row["offered_count"] for row in store.discovery.get_discovered_tracks()}
        if mix_offers is None:
            mix_offers = counts  # The inline Fresh mix is an actual offer; tiles wait for visibility.
        else:
            assert counts == mix_offers
    assert store.discovery.get_discovered_albums()[0]["offered_count"] == 0
    assert store.discovery.get_discovered_artists()[0]["offered_count"] == 0


def test_song_only_pool_fills_discover_and_honors_current_exclusions(store):
    seed_songs(store)
    store.rec_model.record_feedback("for_you", "song 0|artist 0", "dismiss", now=2)
    store.rec_model.record_feedback("for_you", "artist:Artist 1", "mute", now=2)
    store.tracks.upsert_track("v2", "Song 2", "Artist 2", None, None)
    client = client_for(store)
    response = client.get("/home/discovery")
    assert response.text.count('data-discovery-kind="track"') == 10
    for i in range(3):
        assert f'data-discovery-id="song {i}|artist {i}"' not in response.text
    assert "Finding new music" not in response.text
    assert "Browse your discovery pool" not in response.text

    key = re.search(r'data-discovery-id="([^"]+)"', response.text)[1]
    payload = {"kind": "track", "ids": [key], "view_id": str(uuid4())}
    assert client.post("/discovery/impressions", json=payload).status_code == 204
    assert client.post("/discovery/impressions", json=payload).status_code == 204
    assert store.discovery.discovered_tracks_by_keys([key])[key]["offered_count"] == 1


def test_discovery_can_show_one_song_without_claiming_a_mix_is_ready(store):
    seed_songs(store, count=1)
    assert fresh_pool.pick(store, 3) == []  # A saved mix still needs at least four songs.
    response = client_for(store).get("/home/discovery")
    assert response.text.count('data-discovery-kind="track"') == 1
    assert "New recommendations will appear" not in response.text


def test_missing_artist_art_is_queued_and_repaired_image_appears_without_reranking(store, monkeypatch):
    store.discovery.upsert_discovered_artist('New artist', 1, [], [], None, 1)
    client = client_for(store)
    queued = []
    monkeypatch.setattr(client.app.state.ctx.rec_worker, 'trigger_artist_images', queued.append)

    def forbidden(*args):
        raise AssertionError('Opening Discover must not fetch artist images')

    monkeypatch.setattr(discover, '_artist_thumb', forbidden)
    response = client.get('/home/discovery')
    aid = store.discovery.get_discovered_artists()[0]['artist_id']
    assert queued == [[aid]]
    assert 'New artist' in response.text
    store.discovery.record_artist_image(aid, 'http://portrait/new.jpg', 3)
    assert 'http://portrait/new.jpg' in client.get('/home/discovery').text
    assert queued == [[aid]]


@pytest.mark.parametrize("busy", ["idle", "rebuild", "discovery"])
def test_empty_discovery_has_one_honest_state_and_no_idle_polling(store, busy):
    response = client_for(store, busy).get("/home/discovery")
    assert response.status_code == 200
    assert response.text.count('role="status"') == 1
    assert response.text.count('hx-get="/home/discovery"') == int(busy != "idle")
    assert ("Finding new music for you" in response.text) == (busy != "idle")
    assert 'href="/discovery"' in response.text
