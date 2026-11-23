"""Saved-session recovery, native queue adoption and playback continuity."""
import json
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from yt_playlist.web import radio_session as radio_module
from yt_playlist.web.radio_session import PlaylistRadio
from yt_playlist.web.radio_playlist_io import PlaylistPending
from tests.radio_session_support import poc, play


_PROVIDER_OBSERVER = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const {frame, provider, navigation} = JSON.parse(process.argv[1]);
const playlist = frame?.playlist || navigation.radioRecovery.playlist;
const watchEndpoint = {videoId: provider.video, playlistId: playlist};
const rows = provider.ids.map(videoId => {
  const primaryRenderer = {playlistPanelVideoRenderer: {videoId, navigationEndpoint: {watchEndpoint}}};
  if (videoId !== provider.video || !provider.counterpart) return primaryRenderer;
  return {playlistPanelVideoWrapperRenderer: {primaryRenderer, counterpart: [{counterpartRenderer: {
    playlistPanelVideoRenderer: {videoId: provider.counterpart},
  }}]}};
});
const queue = {items: rows, selectedItemIndex: provider.ids.indexOf(provider.video), autoplay: false};
let fetches = 0, navigated = null;
const elements = {
  'ytmusic-app': {polymerController: {
    getState: () => ({playerPage: {playerPageWatchNextResponse: {currentVideoEndpoint: {watchEndpoint}}}}),
    handleQueueUpdateCommand: () => { fetches++; },
  }},
  'ytmusic-player-queue': {queue: {store: {store: {getState: () => ({queue})}}}},
  '#movie_player': {
    getVideoData: () => ({video_id: provider.status ? '' : provider.video}),
    getPlaylistId: () => provider.status ? '' : playlist,
    getPlayerState: () => provider.ended ? 0 : provider.paused ? 2 : 1,
    getPlayerResponse: () => ({playabilityStatus: {status: provider.status || 'OK'}}),
  },
  video: {paused: provider.paused, currentTime: 30, duration: 300},
};
const context = vm.createContext({
  window: {}, URL, AbortController, Date, setTimeout() {}, clearTimeout() {},
  createNativeRatings: () => ({}),
  location: {href: 'https://music.youtube.com/watch?v=' + provider.video + '&list=' + playlist},
  document: {querySelector: key => elements[key], querySelectorAll: () => [], addEventListener() {}},
  chrome: {
    scripting: {executeScript({files, func, args}) {
      if (files) vm.runInContext(fs.readFileSync('extension/' + files[0], 'utf8'), context);
      else return [{result: func(...args)}];
    }},
    tabs: {update(id, {url}) { navigated = url; }},
  },
});
if (navigation) {
  for (const file of ['radio_session.js', 'worker/playback.js'])
    vm.runInContext(fs.readFileSync('extension/' + file, 'utf8'), context);
  const playback = vm.runInContext('createPlayback', context)({
    origin: 'https://music.youtube.com',
    musicTabs: {list: () => [{id: 1}], coordinate: operation => operation(), suppressBeforeUnload() {}},
    warn: (message, error) => { throw error; },
  });
  playback.navigate(navigation.url, null, navigation.radioRecovery)
    .then(() => console.log(JSON.stringify({navigated})));
} else {
  vm.runInContext(fs.readFileSync('extension/worker/radio.js', 'utf8'), context);
  const observer = vm.runInContext('createRadioSession', context)({pickTab: () => ({id: 1})});
  observer.update(frame).then(report => console.log(JSON.stringify({report, fetches})));
}
"""


@pytest.mark.parametrize('outcome', [
    'repaired', 'counterpart', 'ended', 'unplayable', 'unplayable_tail', 'unfamiliar_status', 'unmapped_unplayable',
    'unmapped_ended', 'unmapped_paused', 'paused', 'superseded', 'native_advanced',
])
def test_on_air_continues_from_the_confirmed_queue(poc, monkeypatch, outcome):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required to exercise the extension')
    controller, remote, frames, *_ = poc
    bridge = controller.ctx.bridge
    provider, fetches = {}, []
    send = bridge.send_control
    blocking_status = 'CONTENT_CHECK_REQUIRED' if outcome == 'unfamiliar_status' else 'UNPLAYABLE'
    blocked_current = outcome in {'unplayable', 'unplayable_tail', 'unfamiliar_status'}

    def provider_call(**message):
        result = subprocess.run([node, '-e', _PROVIDER_OBSERVER, json.dumps({'provider': provider, **message})],
                                capture_output=True, text=True, check=True, timeout=10)
        return json.loads(result.stdout)

    def navigate(frame, **options):
        sent = send(frame, **options)
        if sent:
            if frame.get('radioRecovery'):
                if outcome == 'native_advanced':
                    provider.update(video='v2', ids=list(remote.ids), paused=False, ended=False)
                dispatched = provider_call(navigation=frame)['navigated']
                if outcome == 'native_advanced':
                    assert dispatched is None
                    return sent
                assert dispatched == frame['url']
            (video,) = parse_qs(urlparse(frame['url']).query)['v']
            if outcome == 'unmapped_unplayable' and video == 'v30':
                # Music can respond to a retry with another unplayable upload
                # and still identify the same saved playlist.
                provider.update(video='v31')
                return sent
            provider.clear()
            provider.update(video=video, ids=[] if video == 'v1' else list(remote.ids))
            if blocked_current and video == 'v2' or outcome == 'unplayable_tail' and video == 'v3':
                provider.update(status=blocking_status, paused=True, ids=[])
        return sent

    async def observe(frame):
        snapshot = provider_call(frame=frame)
        if snapshot['fetches']:
            fetches.append(provider['video'])
            if outcome in {'repaired', 'counterpart'}:
                provider['ids'] = [provider['video'], *remote.ids[1 if provider['video'] == 'v1' else 2:]]
        return 200, json.dumps(snapshot['report'])

    monkeypatch.setattr(bridge, 'send_control', navigate)
    monkeypatch.setattr(bridge, 'request_control', observe)
    assert controller.start()['ok']
    session = controller.session
    clock = time.monotonic()
    elapsed = 0
    monkeypatch.setattr(radio_module, 'time', SimpleNamespace(monotonic=lambda: clock + elapsed))

    def tick(seconds):
        nonlocal elapsed
        elapsed += seconds
        controller.listening.observe(session)
        controller.advance(session)

    tick(0)
    tick(2)
    assert fetches[0] == 'v1'
    assert [track['video_id'] for track in session.heard] == ['v1']
    assert len(frames) == 1
    if outcome in {'repaired', 'counterpart'}:
        tick(2)
        assert session.browser['playback_synced']
        if outcome == 'counterpart':
            provider.update(video='v30', ids=['v30'], counterpart='v2')
            tick(2)
            assert session.browser['savedIndex'] == 1
            tick(2)
            tick(2)
            assert session.browser['playback_synced']
        else:
            provider.update(video='v2')
            tick(2)
        assert [t['video_id'] for t in session.heard] == ['v1', 'v30' if outcome == 'counterpart' else 'v2']
        assert len(frames) == 1
    elif blocked_current:
        provider.update(video='v2', status=blocking_status, paused=True)
        tick(2)
        assert session.browser['player']['error'] == blocking_status
        tick(4)
        assert frames[-1]['url'].endswith('v=v2&list=PLSESSION'), 'retry the current track first'
        assert len(frames) == 2
        tick(12)
        assert provider['video'] == 'v3'
        tick(2)
        if outcome == 'unplayable_tail':
            tick(4)
            tick(12)
            assert len(frames) == 4, 'retry the last saved song before refilling'
            assert remote.ids[-1] == 'v4'
            tick(2)
            tick(2)
            assert [t['video_id'] for t in session.heard] == ['v1', 'v4']
            assert len(frames) == 5
        else:
            assert [t['video_id'] for t in session.heard] == ['v1', 'v3']
            assert len(frames) == 3
    elif outcome == 'unmapped_unplayable':
        provider.update(video='v30', ids=[], status='UNPLAYABLE', paused=True)
        tick(2)
        assert session.browser['player']['playlist_ytm_id'] == session.playlist
        assert session.browser['player']['error'] == 'UNPLAYABLE'
        assert session.browser['savedIndex'] is None
        tick(4)
        assert frames[-1]['url'].endswith('v=v30&list=PLSESSION')
        assert len(frames) == 2
        tick(12)
        tick(4)
        assert provider['video'] == 'v2'
        tick(2)
        assert [track['video_id'] for track in session.heard] == ['v1', 'v2']
        assert len(frames) == 3
    elif outcome in {'unmapped_ended', 'unmapped_paused'}:
        provider.update(video='v30', ids=[])
        tick(2)
        assert len(frames) == 1, 'an unmapped upload that is playing can continue'
        ended = outcome == 'unmapped_ended'
        provider.update(paused=True, ended=ended)
        tick(2)
        assert session.browser['savedIndex'] is None
        assert session.browser['player']['ended'] is ended
        tick(4)
        tick(2)
        assert len(frames) == (2 if ended else 1)
        assert [track['video_id'] for track in session.heard] == (['v1', 'v2'] if ended else ['v1'])
    else:
        provider.update(paused=True, ended=outcome != 'paused')
        tick(2)
        if outcome == 'superseded':
            bridge.begin_playback_intent()
        tick(4)
        tick(2)
        if outcome in {'ended', 'native_advanced'}:
            assert provider['video'] == 'v2'
            assert [t['video_id'] for t in session.heard] == ['v1', 'v2']
            assert len(frames) == 2
        else:
            assert len(frames) == 1
    assert remote.ids[:3] == ['v1', 'v2', 'v3']
    assert not remote.removes


def test_checkpoint_only_cancel_survives_late_playback_and_process_reopen(poc, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tests.prepared_fixture_support import capture_template
    from yt_playlist.core.store import Store
    from yt_playlist.web.routes import radio_session

    controller, remote, frames, *_ = poc
    launch_id = 'checkpoint-only-cancel'
    with controller.launch_request(launch_id) as launch:
        assert controller.start(launch=launch)['ok']
    initial, revision = controller.ctx.store.radio_checkpoints.load()
    before_remote = json.dumps((remote.ids, remote.adds, remote.removes, frames))
    controller.suspend()
    controller.ctx.bridge.now_playing = None
    controller.ctx.bridge.now_playing_seen_at = None
    restored = PlaylistRadio(controller.ctx)
    controller.ctx.radio = restored
    app = FastAPI()
    app.include_router(radio_session.build(controller.ctx))
    with TestClient(app) as client:
        assert client.post('/radio/stop', json={'launch_id': 'different-launch'}).json()['ok']
        assert controller.ctx.store.radio_checkpoints.load() == (initial, revision)
        response = client.post('/radio/stop', json={'launch_id': launch_id})
        assert response.status_code == 200 and response.json()['ok']
        stopped, stopped_revision = controller.ctx.store.radio_checkpoints.load()
        assert stopped['stopped'] and stopped['phase'] == 'stopped'
        assert {key: value for key, value in stopped.items() if key not in {'phase', 'stopped'}} == {
            key: value for key, value in initial.items() if key not in {'phase', 'stopped'}}
        assert client.post('/radio/stop', json={'launch_id': launch_id}).json()['ok']
        assert controller.ctx.store.radio_checkpoints.load() == (stopped, stopped_revision)
    bridge = controller.ctx.bridge
    bridge.now_playing = {'video_id': remote.ids[0], 'playlist_ytm_id': initial['playlist'], 'paused': False}
    bridge.now_playing_seen_at = time.monotonic()
    assert not restored.join()['active']
    another = PlaylistRadio(controller.ctx)
    assert not another.join()['active']
    assert json.dumps((remote.ids, remote.adds, remote.removes, frames)) == before_remote

    # Copy the committed private state, including its owned files. The child
    # receives no cancellation map or live controller from this process.
    database = tmp_path / 'cancelled-radio.db'
    template = capture_template(controller.ctx.store, None)
    target = Store(database)
    try:
        template.restore(target)
        target._bind_repositories()
    finally:
        target.conn.close()
    child = subprocess.run([sys.executable, '-c', r'''
import json, sys, time
from types import SimpleNamespace
from yt_playlist.core.store import Store
from yt_playlist.web.radio_session import PlaylistRadio
store = Store.open_current(sys.argv[1])
radio = None
try:
    saved, revision = store.radio_checkpoints.load()
    bridge = SimpleNamespace(connected=True, now_playing={
        'video_id': saved['tracks'][0]['video_id'], 'playlist_ytm_id': saved['playlist'], 'paused': False},
        now_playing_seen_at=time.monotonic())
    radio = PlaylistRadio(SimpleNamespace(store=store, bridge=bridge,
        client_provider=lambda: {saved['identity']: object()}, now_fn=lambda: 1234.))
    def unexpected(*args, **kwargs):
        raise AssertionError('A cancelled checkpoint must not start monitoring or navigate')
    radio.work.start_thread = bridge.send_control = unexpected
    assert not radio.cancelled_launches
    state = radio.join()
    assert not state['active'] and radio.session is None
    assert saved['stopped'] and saved['launch_id'] == sys.argv[2]
    assert store.radio_checkpoints.load() == (saved, revision)
    assert not list(store.conn.execute('PRAGMA foreign_key_check'))
    print(json.dumps({'active': state['active'], 'stopped': saved['stopped']}))
finally:
    if radio:
        radio.work.stop_executor()
    store.conn.close()
''', str(database), launch_id], capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stdout + child.stderr
    assert json.loads(child.stdout) == {'active': False, 'stopped': True}


def test_append_uses_native_observation_instead_of_stale_bridge_video(poc):
    c, remote, *_ = poc
    c.start()
    play(c, "v1")
    c.ctx.bridge.now_playing["video_id"] = "v3"
    c.advance(c.session)
    assert remote.adds == []
    c.listening.observe(c.session)
    c.advance(c.session)
    assert remote.adds == [["v4"]]


def test_legacy_trial_session_restores_visible_plans_and_republishes_without_assignment(poc):
    from copy import deepcopy
    c, remote, *_ = poc
    c.start()
    play(c, 'v2')
    c.suspend()
    c._persist(c.session)
    before = deepcopy(c.session.directions.slots)
    checkpoint = c.ctx.store.radio_checkpoints
    saved, revision = checkpoint.load()
    saved['fingerprint'] = 'previous-cache'
    saved['comparison'] = {'run_id': 7, 'arm': 'challenger', 'unit': 'old-day'}
    saved['candidate_context']['choice_context']['comparison'] = saved['comparison']
    checkpoint.save(json.dumps(saved), expected=revision)
    restored = PlaylistRadio(c.ctx)
    assert restored.join()['active']
    assert 'comparison' not in restored.session.candidates.choice_context
    assert restored.session.directions.slots == before
    assert remote.adds == []
    restored._persist(restored.session)
    current = checkpoint.load()[0]
    assert 'comparison' not in current
    assert 'comparison' not in current['candidate_context']['choice_context']


def test_refresh_request_is_not_acknowledgment_and_late_drift_is_repaired(poc):
    c, remote, *_ = poc
    c.start()
    bridge = c.ctx.bridge
    bridge.auto_adopt = False
    play(c, "v3")
    assert remote.ids == ["v1", "v2", "v3", "v4"]
    assert bridge.requests[-1]["refresh"]
    assert c.session.refresh_needed
    assert not c.status()["browser"]["playback_synced"]
    bridge.native_ids = list(remote.ids)
    c.listening.observe(c.session)
    assert not c.session.refresh_needed
    c.listening.observe(c.session)
    assert not bridge.requests[-1]["refresh"]
    bridge.native_ids = ["v1", "v2", "v3", "old-next"]
    c.listening.observe(c.session)
    assert c.session.refresh_needed
    c.listening.observe(c.session)
    assert bridge.requests[-1]["refresh"]
    assert remote.adds == [["v4"]]


@pytest.mark.parametrize("presence", ["missing", "other_playlist", "paused", "stale", "disconnected", "matching"])
def test_saved_session_is_restored_only_for_fresh_matching_playback(poc, presence):
    c, remote, frames, workers, *_ = poc
    c.start()
    play(c, "v2")
    c.suspend()
    c._persist(c.session)
    bridge = c.ctx.bridge
    if presence == "missing":
        bridge.now_playing = None
    elif presence == "other_playlist":
        bridge.now_playing["playlist_ytm_id"] = "OTHER"
    elif presence == "paused":
        bridge.now_playing["paused"] = True
    elif presence == "stale":
        bridge.now_playing_seen_at = time.monotonic() - 20
    elif presence == "disconnected":
        bridge.connected = False
    restored = PlaylistRadio(c.ctx)
    assert restored.bootstrap()["state"]["active"] is (presence == "matching")
    state = restored.join()
    assert state["active"] is (presence == "matching")
    assert (restored.session is not None) is (presence == "matching")
    assert len(workers) == (2 if presence == "matching" else 1)
    assert len(frames) == 1
    assert remote.adds == []


@pytest.mark.parametrize("presence", ["missing", "matching", "paused", "other_playlist", "no_music_tab", "stopped"])
def test_saved_session_is_usable_immediately_only_for_confirmed_playback(poc, presence):
    c, *_ = poc
    c.start()
    play(c, "v2")
    c._persist(c.session)
    bridge = c.ctx.bridge
    if presence == "missing":
        bridge.now_playing = None
        bridge.now_playing_seen_at = None
    elif presence == "paused":
        bridge.now_playing["paused"] = True
    elif presence == "other_playlist":
        bridge.now_playing["playlist_ytm_id"] = "OTHER"
    elif presence == "no_music_tab":
        bridge.now_playing = None
        bridge.now_playing_seen_at = None
        bridge.sensor_health = {"ytm_tabs": 0, "recovering": False}
    elif presence == "stopped":
        c.stop()
    restored = PlaylistRadio(c.ctx)
    before = restored.bootstrap()["state"]
    assert not before.get("resolving")
    assert before["active"] is (presence == "matching")
    if presence in {"missing", "matching"}:
        if presence == "missing":
            bridge.now_playing = {"video_id": "v2", "playlist_ytm_id": "PLSESSION", "paused": False}
            bridge.now_playing_seen_at = time.monotonic()
        restored.join()
        after = restored.bootstrap()["state"]
        assert not after.get("resolving")
        assert after["active"]


def test_observer_negotiates_legacy_command_only_for_allowlist_rejection(poc):
    c, *_ = poc
    c.start()
    bridge = c.ctx.bridge
    observe = bridge.request_control
    commands = []

    async def old_worker(frame):
        commands.append(frame["type"])
        if frame["type"] == "radio-session":
            return 0, json.dumps({"error": "blocked by allowlist"})
        return await observe(frame)

    bridge.request_control = old_worker
    play(c, "v1")
    assert commands == ["radio-session", "playlist-session-poc"]

    async def unavailable(frame):
        commands.append(frame["type"])
        return 503, json.dumps({"error": "disconnected"})

    commands.clear()
    bridge.request_control = unavailable
    with pytest.raises(PlaylistPending):
        c.listening.observe(c.session)
    assert commands == ["radio-session"]
    assert c.status()["observer_error"] == "disconnected"


def test_playing_song_moves_into_history_even_while_radio_is_halted(poc):
    from yt_playlist.web.radio_state import Phase

    c, remote, *_ = poc
    c.start()
    play(c, "v1")
    c.session.move(Phase.CONFLICT)
    c.ctx.bridge.now_playing = {"video_id": "v3", "playlist_ytm_id": "PLSESSION", "paused": False}
    c.ctx.bridge.now_playing_seen_at = time.monotonic()
    c.listening.observe(c.session)
    assert c.status()["position"] == 2
    assert c.status()["history_count"] == 3
    assert c.session.halted
    assert not remote.adds
    assert all(t["video_id"] != "v3" for branch in c.status()["directions"] for t in branch["tracks"])


def test_playback_can_reach_an_append_whose_confirmation_was_lost(poc):
    c, remote, *_ = poc
    c.start()
    remote.after_add = lambda: setattr(remote, "fail_read", True)
    with pytest.raises(RuntimeError, match="Read response lost"):
        play(c, "v3")
    remote.after_add = lambda: None
    play(c, "v4")
    assert remote.ids == ["v1", "v2", "v3", "v4", "v5"]
    assert remote.adds == [["v4"], ["v5"]]
    assert c.status()["ahead"] == 1


def test_transient_wrong_playlist_context_on_owned_song_keeps_radio(poc):
    c, *_ = poc
    c.start()
    play(c, "v1")
    c.ctx.bridge.now_playing["playlist_ytm_id"] = "PREVIOUS"
    assert c.status()["active"]
    c.ctx.bridge.now_playing["video_id"] = "another-song"
    assert not c.status()["active"]


def test_temporary_unrecognized_readback_retries_without_writing_over_it(poc):
    c, remote, *_ = poc
    c.start()
    play(c, "v2")
    remote.ids.append("temporary-extra")
    c.ctx.bridge.now_playing["video_id"] = "v3"
    c.listening.observe(c.session)
    with pytest.raises(PlaylistPending):
        c.advance(c.session)
    assert c.status()["active"] and not c.session.halted
    assert not remote.adds
    remote.ids.remove("temporary-extra")
    c.advance(c.session)
    assert remote.ids == ["v1", "v2", "v3", "v4"]
    assert remote.adds == [["v4"]]


