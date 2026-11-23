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

from yt_playlist.core.bridge import BridgeError
from yt_playlist.library import generated_acquisition
from yt_playlist.web.routes import radio_session as radio_routes
from tests.radio_session_support import (
    uncontended_poc as route_poc, completed_radio_schema, completed_radio_store,
    completed_poc as poc, play,
)

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


def test_routes_use_independent_playlist_session(route_poc):
    c, remote, *_ = route_poc
    c.ctx.store.settings.set_setting('onboard_listening_format', 'radio')
    offer = c.seeds()
    c.ctx.templates = Jinja2Templates(directory="src/yt_playlist/web/templates")
    app = FastAPI()
    app.include_router(radio_routes.build(c.ctx))
    with TestClient(app) as client:
        assert client.get("/radio/status").json()["active"] is False
        assert client.post("/radio/start", json={
            'video_id': offer['tracks'][0]['video_id'], 'menu_id': offer['menu_id']}).json()["ok"]
        assert remote.ids == ["v1"]
        play(c, "v1")
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
        'header': {'id': session.episode_id, 'revision': 1,
            'account_id': 1, 'status': 'playing', 'recovery_filename': 'session.json',
            'recovery_digest': 'stored-digest', 'recovery_bytes': len(encoded.encode()), 'operation_json': None},
        'queue': [{'position': i, 'video_id': track['video_id'],
                   'identity_key': track['key'], 'state': 'current'} for i, track in enumerate(tracks)]}
    checkpoints.launch_receipts.return_value = {}
    checkpoints.pending_seeded_launch.return_value = None
    files = SimpleNamespace(retained=nullcontext, read=Mock(side_effect=lambda *a, **kw: json.loads(encoded)))
    settings = Mock(spec_set=SettingsRepo)
    store = SimpleNamespace(settings=Settings(settings, files, files), tracks=None, activity_paused=False,
        playlists=Playlists(Mock(spec_set=PlaylistRepo), None, None, nullcontext),
        radio_checkpoints=RadioCheckpoints(checkpoints, None, files, nullcontext))
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
    create = generated_acquisition.create_generated_playlist
    monkeypatch.setattr(generated_acquisition, "create_generated_playlist", lambda store, title, tracks, *args, **kwargs:
                        create(store, title, tracks[1:], *args, **kwargs))
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


def test_playlist_seed_route_uses_playlist_and_rejects_conflicting_seed(route_poc):
    c, remote, *_ = route_poc
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


@pytest.fixture
def playlist_start(route_poc):
    controller, *_ = route_poc
    store = controller.ctx.store
    store.rec_proposals.put_proposals('listening_choices', {
        'home_format': {'episodes': {'radio': 1}}}, 1000)
    account = store.accounts.get_master_account().id
    playlist = store.playlists.upsert_playlist(account, 'PLSOURCE', 'My mix', 1, '', 1)
    store.playlists.set_playlist_tracks(playlist, [store.tracks.track_ids_for_videos(['v8'])['v8']])
    return {'playlist_id': playlist, 'launch_id': 'playlist-launch'}


def test_retry_of_playlist_radio_launch_reuses_session(route_poc, playlist_start):
    controller, remote, frames, *_ = route_poc
    app = FastAPI()
    app.include_router(radio_routes.build(controller.ctx))
    request = playlist_start
    with TestClient(app) as client:
        first = client.post('/radio/start', json=request).json()
        assert first['ok'], first
        session = controller.session
        retry = client.post('/radio/start', json=request).json()
        assert retry['ok'], retry
        assert (len(remote.creations), len(frames)) == (1, 1)
        assert controller.session is session
        new_launch = client.post('/radio/start', json={**request, 'launch_id': 'new-playlist-launch'}).json()
        assert new_launch['ok'], new_launch
        newest = controller.session
        serial = controller.tuning_serial
        retry = client.post('/radio/start', json=request).json()
        assert controller.session is newest
        assert retry['launch_id'] == 'new-playlist-launch'
        assert controller.tuning_serial == serial
    assert (len(remote.creations), len(frames)) == (2, 2)
    assert controller.session is not session


def test_retry_of_playlist_radio_launch_after_reopen_reuses_saved_episode(route_poc, playlist_start):
    controller, remote, frames, *_ = route_poc
    request = playlist_start
    app = FastAPI()
    app.include_router(radio_routes.build(controller.ctx))
    with TestClient(app) as client:
        first = client.post('/radio/start', json=request).json()
        assert first['ok'], first
    saved = controller.persistence.load()
    assert saved['launch_id'] == request['launch_id']
    controller.suspend()
    restored = type(controller)(controller.ctx)
    restored.ctx.radio = restored
    reopened_app = FastAPI()
    reopened_app.include_router(radio_routes.build(restored.ctx))
    try:
        with TestClient(reopened_app) as client:
            retry = client.post('/radio/start', json=request).json()
        assert (len(remote.creations), len(frames)) == (1, 1)
        assert retry['ok'] and retry.get('joined') and not retry['active'], retry
        assert retry['resume_player']['radio_session_id'] == saved['episode_id']
        assert restored.persistence.load()['episode_id'] == saved['episode_id']
    finally:
        restored.suspend()
        restored.work.stop_executor()


def test_older_playlist_radio_retry_after_reopen_keeps_newer_checkpoint(route_poc, playlist_start):
    controller, remote, frames, *_ = route_poc
    store = controller.ctx.store
    first = playlist_start
    newest = {**first, 'launch_id': 'later-playlist-launch'}
    app = FastAPI()
    app.include_router(radio_routes.build(controller.ctx))
    with TestClient(app) as client:
        assert client.post('/radio/start', json=first).json()['ok']
        assert client.post('/radio/start', json=newest).json()['ok']
    checkpoint = store.radio_checkpoints.load()
    controller.suspend()
    restored = type(controller)(controller.ctx)
    restored.ctx.radio = restored
    reopened_app = FastAPI()
    reopened_app.include_router(radio_routes.build(restored.ctx))
    with TestClient(reopened_app) as client:
        client.post('/radio/start', json=first).raise_for_status()
        retry = client.post('/radio/start', json=newest).json()
    assert retry['ok'] and retry['joined'], retry
    assert retry['resume_player']['radio_session_id'] == checkpoint[0]['episode_id']
    assert restored.tuning_serial == 0
    assert store.radio_checkpoints.load() == checkpoint
    assert len(remote.creations) == len(frames) == 2


def test_radio_start_retry_preserves_later_song_choice(route_poc, playlist_start):
    controller, remote, frames, *_ = route_poc
    request = playlist_start
    app = FastAPI()
    app.include_router(radio_routes.build(controller.ctx))
    with TestClient(app) as client:
        assert client.post('/radio/start', json=request).json()['ok']
        session = controller.session
        play(controller, 'v8')
        state = client.get('/radio/status').json()
        branch = next(direction for direction in state['directions'] if direction['active'])
        target = branch['tracks'][2]['video_id']
        selection = {'proposal_id': branch['id'], 'menu_id': state['menu_id'], 'video_id': target}
        assert client.post('/radio/select', json=selection).json()['ok']
        serial = controller.tuning_serial
        assert client.post('/radio/start', json=request).json()['joined']
        assert controller.session is session and controller.tuning_serial == serial
        assert len(remote.creations) == len(frames) == 1
        controller.advance(session)
    assert target in remote.ids
    assert len(frames) == 2
    assert frames[-1]['url'].endswith(f'v={target}&list=PLSESSION')


def test_radio_retry_after_lost_create_response_does_not_repeat_provider_write(route_poc, playlist_start, monkeypatch):
    controller, remote, frames, *_ = route_poc
    store = controller.ctx.store
    request = playlist_start
    app = FastAPI()
    app.include_router(radio_routes.build(controller.ctx))
    create = generated_acquisition.create_generated_playlist

    def lose_response(*args, **kwargs):
        create(*args, **kwargs)
        raise TimeoutError('Provider response was lost')

    with monkeypatch.context() as provider:
        provider.setattr(generated_acquisition, 'create_generated_playlist', lose_response)
        with TestClient(app) as client:
            result = client.post('/radio/start', json=request).json()
            assert not result['ok'] and result['reason'] == 'Provider response was lost'
            assert len(remote.creations) == 1 and not frames
    assert store.radio_checkpoints.load()[0] is None
    restored = type(controller)(controller.ctx)
    restored.ctx.radio = restored
    reopened_app = FastAPI()
    reopened_app.include_router(radio_routes.build(restored.ctx))
    with TestClient(reopened_app) as client:
        assert not client.post('/radio/start', json=request).json()['ok']
        assert len(remote.creations) == 1 and not frames
        assert client.post('/radio/start', json={**request, 'launch_id': 'explicit-new'}).json()['ok']
    assert len(remote.creations) == 2 and len(frames) == 1


class CreateInterrupted(BaseException):
    """Simulate process exit before Start can handle the provider response."""


@pytest.mark.parametrize(('failure', 'reopen'), [(TimeoutError, False), (TimeoutError, True),
                                                (CreateInterrupted, True), (None, True)])
def test_seeded_radio_retry_adopts_the_original_provider_playlist(route_poc, monkeypatch, reopen, failure):
    controller, remote, frames, *_ = route_poc
    ctx = controller.ctx
    ctx.store.settings.set_setting('onboard_listening_format', 'radio')
    offer = controller.seeds()
    request = {'launch_id': 'seeded-radio-start', 'video_id': offer['tracks'][0]['video_id'],
               'menu_id': offer['menu_id']}
    saved = {}
    visible = ['PL_DECOY']

    def create(title, description, *, video_ids):
        assert not saved, 'Retry issued another create'
        recovery = ctx.store.radio_checkpoints.pending_seeded_launch()
        receipts = ctx.store.radio_checkpoints.launch_receipts()
        assert recovery['launch_id'] == request['launch_id']
        assert receipts[request['launch_id']] == recovery['episode_id']
        detail = {'title': title, 'description': description, 'trackCount': 1,
                  'tracks': [{'videoId': video_id} for video_id in video_ids]}
        saved.update(PL_ORIGINAL=detail, PL_DECOY={**detail, 'description': 'Generated by TuneConsole'})
        if failure is not None:
            raise failure('Confirmation lost after creation')
        return 'PL_ORIGINAL'

    def listing(*, limit):
        return [{'playlistId': key, 'title': saved[key]['title']} for key in visible]

    monkeypatch.setattr(remote, 'create_playlist', create)
    monkeypatch.setattr(remote, 'get_library_playlists', listing, raising=False)
    monkeypatch.setattr(remote, 'get_playlist', lambda playlist, limit: saved[playlist])
    app = FastAPI()
    app.include_router(radio_routes.build(ctx))
    if failure is CreateInterrupted:
        with controller.launch_request(request['launch_id']) as launch, pytest.raises(CreateInterrupted):
            controller.start(request['video_id'], menu_id=request['menu_id'], launch=launch)
    else:
        with monkeypatch.context() as first, TestClient(app) as client:
            if failure is None:
                first.setattr(ctx.bridge, 'send_control', Mock(side_effect=
                    BridgeError('Extension disconnected before handoff', outcome='not-dispatched')))
            reply = client.post('/radio/start', json=request).json()
        assert reply['ok'] is False and reply['launch_outcome'] == 'uncertain'
    assert not frames
    if reopen:
        controller = ctx.radio = type(controller)(ctx)
        app = FastAPI()
        app.include_router(radio_routes.build(ctx))
    with TestClient(app) as client:
        if failure is not None:
            reply = client.post('/radio/start', json=request).json()
            assert reply['ok'] is False and reply['launch_outcome'] == 'uncertain'
            assert not frames
            visible.append('PL_ORIGINAL')
        with monkeypatch.context() as bridge:
            def reject_after_preparing(frame, *, intent):
                assert frame.prepare()
                controller.suspend()
                raise BridgeError('no extension connected', outcome='not-dispatched')

            if reopen:
                unavailable = Mock(side_effect=reject_after_preparing)
            else:
                unavailable = Mock(return_value=False)
            bridge.setattr(ctx.bridge, 'send_control', unavailable)
            reply = client.post('/radio/start', json=request).json()
            assert reply['ok'] is False and reply['launch_outcome'] == 'uncertain'
            recovery = ctx.store.radio_checkpoints.pending_seeded_launch()
            assert recovery['launch_id'] == request['launch_id']
            assert recovery['playlist'] == 'PL_ORIGINAL' and not recovery['dispatch_started']
            unavailable.assert_called_once()
        reply = client.post('/radio/start', json=request).json()
        assert reply['ok'] and reply['playlist_id'] == 'PL_ORIGINAL', reply
        assert client.post('/radio/start', json=request).json()['playlist_id'] == 'PL_ORIGINAL'
    assert len(frames) == 1 and frames[0]['url'].endswith('&list=PL_ORIGINAL')
    recovery = ctx.store.radio_checkpoints.pending_seeded_launch()
    assert recovery['playlist'] == 'PL_ORIGINAL' and recovery['dispatch_started']


@pytest.mark.parametrize('player', ['before_queue', 'missing', 'wrong_song', 'wrong_playlist', 'stale', 'stopped'])
def test_seeded_radio_restart_reconciles_dispatch_without_replaying(route_poc, monkeypatch, player):
    controller, remote, frames, *_ = route_poc
    ctx = controller.ctx
    ctx.store.settings.set_setting('onboard_listening_format', 'radio')
    offer = controller.seeds()
    seed = offer['tracks'][0]['video_id']
    request = {'launch_id': 'seeded-before-exit', 'video_id': seed, 'menu_id': offer['menu_id']}
    send_control = ctx.bridge.send_control

    def interrupt(frame, *, intent):
        if player != 'before_queue':
            assert send_control(frame, intent=intent)
        raise CreateInterrupted('Process exited during playback handoff')

    with monkeypatch.context() as bridge:
        bridge.setattr(ctx.bridge, 'send_control', interrupt)
        with controller.launch_request(request['launch_id']) as launch, pytest.raises(CreateInterrupted):
            controller.start(seed, menu_id=request['menu_id'], launch=launch)
    recovery = ctx.store.radio_checkpoints.pending_seeded_launch()
    assert recovery['playlist'] == 'PLSESSION'
    assert bool(recovery['dispatch_started']) == (player != 'before_queue')

    ctx.radio = type(controller)(ctx)
    app = FastAPI()
    app.include_router(radio_routes.build(ctx))
    exact = {'playlist_ytm_id': 'PLSESSION', 'video_id': seed, 'paused': False}
    observed = {
        'wrong_song': {**exact, 'video_id': 'different'},
        'wrong_playlist': {**exact, 'playlist_ytm_id': 'PL_OTHER'},
        'stale': exact,
    }
    ctx.bridge.now_playing = observed.get(player)
    ctx.bridge.now_playing_seen_at = time.monotonic() - (9 if player == 'stale' else 0)
    newer_intent = ctx.bridge.begin_playback_intent()
    with TestClient(app) as client:
        if player != 'before_queue':
            for _ in range(2):
                result = client.post('/radio/start', json=request).json()
                assert not result['ok'] and result['launch_outcome'] == 'uncertain'
            assert ctx.radio.session is None
            assert ctx.bridge.playback_intent_current(newer_intent)
            if player == 'stopped':
                assert client.post('/radio/stop', json={'launch_id': request['launch_id']}).json()['ok']
                assert ctx.store.radio_checkpoints.pending_seeded_launch() is None
            ctx.bridge.now_playing = exact
            ctx.bridge.now_playing_seen_at = time.monotonic()
        result = client.post('/radio/start', json=request).json()
        if player == 'stopped':
            assert not result['ok']
        else:
            assert result['ok'] and result['playlist_id'] == 'PLSESSION', result
            assert ctx.radio.session.episode_id == recovery['episode_id']
            assert client.post('/radio/start', json=request).json()['joined']
        if player != 'before_queue':
            assert ctx.bridge.playback_intent_current(newer_intent)
    assert len(remote.creations) == len(frames) == 1


def test_seeded_radio_restart_prefers_published_checkpoint_over_recovery(route_poc, monkeypatch):
    controller, remote, frames, *_ = route_poc
    ctx = controller.ctx
    checkpoints = ctx.store.radio_checkpoints
    ctx.store.settings.set_setting('onboard_listening_format', 'radio')
    offer = controller.seeds()
    seed = offer['tracks'][0]['video_id']
    request = {'launch_id': 'seeded-with-saved-checkpoint', 'video_id': seed, 'menu_id': offer['menu_id']}
    app = FastAPI()
    app.include_router(radio_routes.build(ctx))
    with TestClient(app) as client:
        assert client.post('/radio/start', json=request).json()['ok']
    with monkeypatch.context() as cleanup:
        cleanup.setattr(checkpoints, 'take_seeded_launch', Mock(return_value=False))
        play(controller, seed)
    recovery = checkpoints.pending_seeded_launch()
    assert recovery['dispatch_started'] and checkpoints.load()[0]['episode_id'] == recovery['episode_id']

    ctx.bridge.now_playing = None
    ctx.radio = type(controller)(ctx)
    reopened = FastAPI()
    reopened.include_router(radio_routes.build(ctx))
    with TestClient(reopened) as client:
        replay = client.post('/radio/start', json=request).json()
    assert replay['ok'] and replay['joined'] and replay['playlist_id'] == 'PLSESSION', replay
    assert len(remote.creations) == len(frames) == 1


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
        response = client.get("/?panel=radio")
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
