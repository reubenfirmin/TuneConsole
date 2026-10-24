"""Radio routes, playlist launch validation and served assets."""
import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from yt_playlist.library import playlist_generated
from yt_playlist.web.routes import radio_session as radio_routes
from tests.radio_session_support import poc, play

@pytest.mark.parametrize("values", [{}, {"comfort": 2, "theme": .5, "energy": .5},
                                   {"comfort": True, "theme": .5, "energy": .5}, []])
def test_tune_route_rejects_invalid_controls_without_changing_session(poc, values):
    c, remote, *_ = poc
    c.start()
    app = FastAPI()
    app.include_router(radio_routes.build(c.ctx))
    with TestClient(app) as client:
        assert client.post("/radio/tune", json=values).json() == {"ok": False}
    assert remote.ids == ["v1", "v2", "v3"]


def test_routes_use_independent_playlist_session(poc, monkeypatch):
    c, remote, *_ = poc
    c.ctx.templates = Jinja2Templates(directory="src/yt_playlist/web/templates")
    app = FastAPI()
    app.include_router(radio_routes.build(c.ctx))
    with TestClient(app) as client:
        assert client.get("/radio/status").json()["active"] is False
        assert client.post("/radio/start").json()["ok"]
        play(c, "v3")
        assert client.get("/radio/status").json()["ahead"] == 1
        assert client.post("/radio/stop").json()["active"] is False
    assert not c.ctx.radio.active
    assert remote.ids == ["v1", "v2", "v3", "v4"]


def test_playlist_seed_preserves_source_order_and_continues_in_a_separate_playlist(poc):
    c, remote, frames, _, _ = poc
    store = c.ctx.store
    iid = store.identities.get_master_identity().id
    pid = store.playlists.upsert_playlist(iid, "PLSOURCE", "My mix", 4, "", 1)
    videos = ["v8", "v5", "v11", "v16"]
    by_video = store.tracks.track_ids_for_videos(videos)
    ids = [by_video[v] for v in videos]
    store.playlists.set_playlist_tracks(pid, ids)
    assert c.start(playlist_id=pid)["ok"]
    assert remote.ids == ["v8", "v5", "v11", "v16"]
    assert c.session.local_id != pid
    assert frames[0]["url"].endswith("v=v8&list=PLSESSION")
    play(c, "v16")
    assert len(remote.ids) == 5
    assert store.playlists.get_playlist_track_ids(pid) == ids
    assert not remote.removes


def test_explicit_playlist_launch_replaces_active_radio_and_validates_before_stopping(poc):
    c, remote, *_ = poc
    c.start()
    play(c, "v1")
    old = c.session
    assert not c.start(playlist_id=999)["ok"]
    assert c.session is old and not old.stop.is_set()
    store = c.ctx.store
    iid = store.identities.get_master_identity().id
    pid = store.playlists.upsert_playlist(iid, "PLSOURCE", "My mix", 1, "", 1)
    assert not c.start(playlist_id=pid)["ok"]
    assert c.session is old and not old.stop.is_set()
    store.playlists.set_playlist_tracks(pid, [store.tracks.track_ids_for_videos(["v8"])["v8"]])
    assert c.start(playlist_id=pid)["ok"]
    assert old.stop.is_set()
    assert c.session is not old
    assert remote.ids[0] == "v8"


def test_playlist_launch_accepts_tracks_outside_prepared_pools_and_skips_unavailable(poc):
    c, remote, *_ = poc
    store = c.ctx.store
    iid = store.identities.get_master_identity().id
    pid = store.playlists.upsert_playlist(iid, "PLSOURCE", "My mix", 2, "", 1)
    unavailable = store.tracks.upsert_track("v8", "Song 8", "Artist 8", "", None, available=0)
    outside = store.tracks.upsert_track("v40", "Outside pool", "New artist", "", 200)
    store.playlists.set_playlist_tracks(pid, [unavailable, outside])
    assert c.start(playlist_id=pid)["ok"]
    assert remote.ids == ["v40", "v1", "v2"]


def test_playlist_launch_waits_for_accepted_tracks_when_writer_omits_a_song(poc, monkeypatch):
    c, remote, *_ = poc
    store = c.ctx.store
    iid = store.identities.get_master_identity().id
    pid = store.playlists.upsert_playlist(iid, "PLSOURCE", "My mix", 4, "", 1)
    videos = ["v8", "v5", "v11", "v16"]
    by_video = store.tracks.track_ids_for_videos(videos)
    store.playlists.set_playlist_tracks(pid, [by_video[v] for v in videos])
    create = playlist_generated.create_generated_playlist
    monkeypatch.setattr(playlist_generated, "create_generated_playlist", lambda store, title, tracks, *args:
                        create(store, title, tracks[1:], *args))
    assert c.start(playlist_id=pid)["ok"]
    assert remote.ids == ["v5", "v11", "v16"]
    assert c.session.initial_tracks == []
    assert c.session.directions.seed["video_id"] == "v5"
    assert set(c.session.choices) == {"v5"}


@pytest.mark.parametrize("playlist_id", [True, False, -1, 0, 1.5, "1", [], {}, 2**63])
def test_playlist_seed_route_rejects_invalid_ids(poc, playlist_id):
    c, remote, frames, *_ = poc
    app = FastAPI()
    app.include_router(radio_routes.build(c.ctx))
    with TestClient(app) as client:
        assert client.post("/radio/start", json={"playlist_id": playlist_id}).json()["ok"] is False
    assert remote.ids == frames == []


def test_playlist_seed_route_uses_playlist_and_rejects_conflicting_seed(poc):
    c, remote, *_ = poc
    store = c.ctx.store
    iid = store.identities.get_master_identity().id
    pid = store.playlists.upsert_playlist(iid, "PLSOURCE", "My mix", 1, "", 1)
    store.playlists.set_playlist_tracks(pid, [store.tracks.track_ids_for_videos(["v8"])["v8"]])
    app = FastAPI()
    app.include_router(radio_routes.build(c.ctx))
    with TestClient(app) as client:
        assert not client.post("/radio/start", json={"playlist_id": pid, "video_id": "v1"}).json()["ok"]
        assert client.post("/radio/start", json={"playlist_id": pid}).json()["ok"]
    assert remote.ids == ["v8", "v1", "v2"]


def test_radio_page_is_registered_and_assets_are_served(store):
    from yt_playlist.web.app import create_app
    from tests.onboarding_support import publish_personal_mix

    store.settings.set_setting("last_sync_at", "1000")
    publish_personal_mix(store)
    with TestClient(create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False),
                    base_url="http://127.0.0.1") as client:
        response = client.get("/#radio")
        assert response.status_code == 200
        assert 'data-dash="radio"' in response.text
        assert 'class="radio-home"' in response.text
        assert response.text.index('/static/radio-artwork.js?v=') < response.text.index('/static/radio.js?v=')
        assert '/static/radio.js?v=' in response.text
        assert '/static/radio.css?v=' in response.text
        assert client.get("/static/radio.js").status_code == 200
        assert client.get("/static/radio-artwork.js").status_code == 200
        assert client.get("/static/radio.css").status_code == 200
        assert not client.get("/radio/status").json()["active"]
