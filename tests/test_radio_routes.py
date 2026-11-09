"""Radio routes, playlist launch validation and served assets."""
import json
import time
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

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
        assert client.post("/radio/tune", json=values).json() == {"ok": False, "tune_reason": "invalid_controls"}
    assert remote.ids == ["v1", "v2", "v3"]


def test_routes_use_independent_playlist_session(poc, monkeypatch):
    c, remote, *_ = poc
    c.ctx.store.settings.set_setting('onboard_listening_format', 'radio')
    offer = c.seeds()
    c.ctx.templates = Jinja2Templates(directory="src/yt_playlist/web/templates")
    app = FastAPI()
    app.include_router(radio_routes.build(c.ctx))
    with TestClient(app) as client:
        assert client.get("/radio/status").json()["active"] is False
        assert client.post("/radio/start", json={
            'video_id': offer['tracks'][0]['video_id'], 'menu_id': offer['menu_id']}).json()["ok"]
        play(c, "v3")
        assert client.get("/radio/status").json()["ahead"] == 1
        assert client.post("/radio/stop").json()["active"] is False
    assert not c.ctx.radio.active
    assert remote.ids == ["v1", "v2", "v3", "v4"]


@pytest.mark.parametrize("restored", [False, True])
def test_status_polls_project_session_without_loading_seed_menu(monkeypatch, restored):
    from yt_playlist.controllers.playlists import Playlists
    from yt_playlist.controllers.radio_checkpoints import RadioCheckpoints
    from yt_playlist.controllers.settings import Settings
    from yt_playlist.rec import radio_tuning
    from yt_playlist.repos.playlists import PlaylistRepo
    from yt_playlist.repos.radio_checkpoints import RadioCheckpointRepo
    from yt_playlist.repos.settings import SettingsRepo
    from yt_playlist.web.radio_persistence import encode
    from yt_playlist.web.radio_session import PlaylistRadio
    from yt_playlist.web.radio_state import Phase, Session
    from yt_playlist.web.routes.bridge import build as build_bridge_route

    tracks = [{"key": f"song{i}|artist", "video_id": f"v{i}",
               "title": f"Song {i}", "artist": "Artist"} for i in range(1, 4)]
    session = Session(None, SimpleNamespace(tuning=radio_tuning.DEFAULTS, fingerprint="status"),
                      1, "Radio Session", phase=Phase.PLAYING, playlist="PLSESSION", local_id=1,
                      tracks=tracks, heard=tracks[:1], position=0, furthest=0, monitoring=True)
    encoded = encode(session)
    checkpoints = Mock(spec_set=RadioCheckpointRepo)
    checkpoints.checkpoint_facts.return_value = {
        'native': True, 'header': {'id': session.episode_id, 'revision': 1,
            'account_id': 1, 'status': 'playing', 'recovery_filename': 'session.json',
            'recovery_digest': 'stored-digest', 'recovery_bytes': len(encoded.encode())},
        'queue': [{'position': i, 'video_id': track['video_id'],
                   'identity_key': track['key'], 'state': 'current'} for i, track in enumerate(tracks)]}
    checkpoints.operation_facts.return_value = None
    files = SimpleNamespace(retained=nullcontext, read=Mock(side_effect=lambda *a, **kw: json.loads(encoded)))
    settings = Mock(spec_set=SettingsRepo)
    store = SimpleNamespace(settings=Settings(settings, files, files), tracks=None, activity_paused=False,
        playlists=Playlists(Mock(spec_set=PlaylistRepo), None, None, nullcontext),
        radio_checkpoints=RadioCheckpoints(checkpoints, None, None, files, nullcontext))
    store.read_view = lambda: nullcontext(store)
    bridge = SimpleNamespace(connected=True, resumable_player=None,
        now_playing={"video_id": "v1", "playlist_ytm_id": "PLSESSION", "paused": False},
        now_playing_seen_at=time.monotonic())
    ctx = SimpleNamespace(store=store, bridge=bridge, now_fn=lambda: 1234.)
    c = ctx.radio = PlaylistRadio(ctx)
    # Scheduling is checked separately; these polls must project an existing
    # live session or a committed checkpoint without preparing recommendations.
    monkeypatch.setattr(c.work, "start_thread", Mock())
    if not restored:
        c.session = session
        c.offer = []
    expected = {"active": True, "playlist_id": "PLSESSION", "title": "Radio Session", "history": tracks[:1]}

    def no_seed_work(*args, **kwargs):
        pytest.fail("Session status performed seed-menu work")

    settings.get_setting.side_effect = no_seed_work
    monkeypatch.setattr(c, "bootstrap", no_seed_work)
    monkeypatch.setattr(c, "_eligible_seeds", no_seed_work)
    checkpoints.reset_mock()
    c.ctx.bridge.resumable_player = None
    app = FastAPI()
    app.include_router(radio_routes.build(c.ctx))
    app.include_router(build_bridge_route(c.ctx))
    with TestClient(app) as client:
        response = client.get("/radio/status")
        assert response.status_code == 200
        state = response.json()
        for key in ("active", "playlist_id", "title", "history"):
            assert state[key] == expected[key]
        bridge = client.get("/bridge/status")
        assert bridge.status_code == 200
        assert bridge.json()["radio"] == expected["active"]
        assert bridge.json()["now_playing"]["source"]["kind"] == "radio"
    assert checkpoints.checkpoint_facts.call_count == (2 if restored else 0)
    settings.get_setting.assert_not_called()


def test_playlist_seed_preserves_source_order_and_continues_in_a_separate_playlist(poc):
    c, remote, frames, _, _ = poc
    store = c.ctx.store
    iid = store.accounts.get_master_account().id
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
    iid = store.accounts.get_master_account().id
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
    iid = store.accounts.get_master_account().id
    pid = store.playlists.upsert_playlist(iid, "PLSOURCE", "My mix", 2, "", 1)
    unavailable = store.tracks.upsert_track("v8", "Song 8", "Artist 8", "", None, available=0)
    outside = store.tracks.upsert_track("v40", "Outside pool", "New artist", "", 200)
    store.playlists.set_playlist_tracks(pid, [unavailable, outside])
    assert c.start(playlist_id=pid)["ok"]
    assert remote.ids == ["v40", "v1", "v2"]


def test_playlist_launch_waits_for_accepted_tracks_when_writer_omits_a_song(poc, monkeypatch):
    c, remote, *_ = poc
    store = c.ctx.store
    iid = store.accounts.get_master_account().id
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
    c.seeds()
    store.rec_proposals.put_proposals('listening_choices', {
        'home_format': {'episodes': {'radio': 1}}}, 1000)
    iid = store.accounts.get_master_account().id
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
    store.settings.set_setting("onboard_dismissed", "1")
    store.rec_proposals.put_proposals('listening_choices', {'home_format': {
        'episodes': {'radio': 1}}}, 1000)
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
