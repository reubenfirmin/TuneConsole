"""Session launch, browser presence and lifecycle constraints."""
import asyncio
import json
import logging
import threading
import time

import pytest
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState
from yt_playlist.core.bridge import Bridge
from yt_playlist.library import playlist_generated
from yt_playlist.web.radio_session import PlaylistRadio
from yt_playlist.web.radio_playlist_io import PlaylistChanged, PlaylistPending
from tests.radio_session_support import (
    completed_poc as poc, completed_radio_schema, completed_radio_store,
    play, radio_controller, seed_completed_pool,
)
from tests.store_fixture_support import (
    _file_consumer_store, bounded_consumer_schema, native_sql_calls, reuse_reporting_reader,
)


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Controller tests never construct the HTTP app or its background loop."""


@pytest.fixture(scope='module')
def append_schema():
    with bounded_consumer_schema(()) as schema:
        yield schema


@pytest.fixture
def append_poc(tmp_path, append_schema, native_sql_calls, monkeypatch):
    # Keep all 32 catalog subjects and actual checkpoint SQL/files. Controller
    # progression consumes an admitted empty schema, independently of bootstrap
    # and per-query diagnostics. Publication snapshots and guards remain real.
    with _file_consumer_store(tmp_path, append_schema) as store:
        with reuse_reporting_reader(store, monkeypatch):
            pool = seed_completed_pool(store)
            with radio_controller(monkeypatch, store, pool) as state:
                yield state


@pytest.mark.parametrize("prior_titles", [
    [],
    ["Radio Session · 2026-10-22"],
    ["Radio Session · 2026-10-22 #1", "Radio Session · 2026-10-22 #2"],
])
def test_radio_session_titles_are_numbered_per_local_day(append_poc, prior_titles):
    c, *_ = append_poc
    store = c.ctx.store
    identity = store.accounts.get_master_account().id
    for index, title in enumerate([*prior_titles, "Radio Session · 2026-10-21 #9",
                                   "Fresh songs - October 22 2026 #9"]):
        store.playlists.upsert_playlist(identity, f"PLPREVIOUS{index}", title, 0, "", 0)

    title = f"Radio Session · 2026-10-22 #{len(prior_titles) + 1}"
    result = c.start(local_date="2026-10-22")
    assert result["ok"] and result["title"] == title
    assert store.playlists.get_playlist(result["local_id"]).title == title
    joined = c.start(local_date="2026-10-22")
    assert joined["joined"] and joined["title"] == title

    c.stop()
    result = c.start(local_date="2026-10-22")
    assert result["ok"]
    assert result["title"] == f"Radio Session · 2026-10-22 #{len(prior_titles) + 2}"
    assert store.playlists.get_playlist(result["local_id"]).title == result["title"]

    c.stop()
    result = c.start(local_date="2026-10-23")
    assert result["ok"] and result["title"] == "Radio Session · 2026-10-23 #1"


def test_three_to_start_then_one_append_per_song_and_no_playback_queue_commands(append_poc):
    c, remote, frames, workers, _ = append_poc
    assert c.start()["ok"]
    assert remote.ids == ["v1", "v2", "v3"]
    assert len(workers) == 1
    assert frames == [{"type": "navigate", "url":
                       "https://music.youtube.com/watch?v=v1&list=PLSESSION"}]
    play(c, "v1")
    assert remote.adds == []
    play(c, "v2")
    assert remote.adds == []
    assert c.status()["ahead"] == 1
    for i in range(3, 8):
        video, previous = remote.ids[-1], list(remote.ids)
        play(c, video)
        assert remote.ids[:-1] == previous
        assert len(set(remote.ids)) == i + 1
        assert c.status()["ahead"] == 1
        play(c, video)  # Duplicate observations cannot append extra songs.
        assert len(remote.adds) == i - 2
    assert len(frames) == 1  # Playback navigation happens only at explicit start.
    assert all(frame["type"] == "radio-session" for frame in c.ctx.bridge.requests)
    assert c.ctx.store.playlists.get_playlist_track_keys(c.session.local_id) == {
        t["key"] for t in c.session.tracks}


def test_skip_forward_catches_up_and_going_back_does_not_append(poc):
    c, remote, *_ = poc
    c.start()
    play(c, "v3")
    assert remote.ids == ["v1", "v2", "v3", "v4"]
    assert c.status()["ahead"] == 1
    play(c, "v1")
    assert remote.adds == [["v4"]]


@pytest.mark.parametrize('retries', [0, 2])
def test_explicit_skip_records_one_request_and_stale_retries_do_not_reject_the_next_song(
        append_poc, monkeypatch, retries):
    c, *_ = append_poc
    c.start()
    play(c, 'v1', position=1)
    advance, attempts = c._advance, []
    waits = []

    def pacing(delay):
        assert not c.session.stop.is_set()
        waits.append(delay)
        return False

    def preparing(s):
        attempts.append(True)
        if len(attempts) <= retries:
            raise PlaylistPending('Controlled native preparation delay')
        return advance(s)

    def next_song(video):
        assert video == 'v1'
        c.ctx.bridge.now_playing = {**c.ctx.bridge.now_playing, 'video_id': 'v2', 'position': 0}
        return 200, {'ok': True}

    monkeypatch.setattr(c, '_advance', preparing)
    monkeypatch.setattr(c.playback, 'next', next_song)
    # Retry pacing is below this durable-intent check. Every preparation
    # attempt and the final playback observation still run separately.
    monkeypatch.setattr(c.session.stop, 'wait', pacing)
    assert c.skip('v1') == {'ok': True}
    assert len(attempts) == retries + 1
    assert waits == [.25] * (retries + 1)
    rows = c.ctx.store.player_events.player_events_since(0, kind='next')
    assert len(rows) == 1
    assert rows[0]['video_id'] == 'v1' and rows[0]['position'] == 1
    assert rows[0]['account_id'] == c.session.identity
    assert c.skip('v1') == {'ok': True}
    assert c.ctx.store.player_events.player_events_since(0, kind='next') == rows


@pytest.mark.parametrize('restored', [False, True])
def test_cached_seed_suggestions_recheck_skip_breaks_and_return_after_expiry(poc, restored):
    c, *_ = poc
    offer = c.seeds()
    track = offer['tracks'][0]
    c.ctx.store.player_events.record_player_event(
        c.ctx.store.accounts.get_master_account().id, 'next', track['video_id'], 1, 200, '', None, 1234)
    if restored:
        c.offer = None
    bootstrap = c.bootstrap()
    assert track['key'] not in {item['key'] for item in bootstrap['seeds']}
    assert bootstrap['seedMenu'] == offer['menu_id']
    if not restored:
        assert track['key'] not in {item['key'] for item in c.seeds()['tracks']}
    c.ctx.now_fn = lambda: 1234 + 86400
    assert track['key'] in {item['key'] for item in c.bootstrap()['seeds']}


@pytest.mark.parametrize("video,playlist,extra", [
    ("v3", "OTHER", {}), ("unknown", "PLSESSION", {}),
    ("v3", "", {}), ("v3", "PLSESSION", {"paused": True}),
])
def test_only_playback_in_this_playlist_advances_it(poc, video, playlist, extra):
    c, remote, *_ = poc
    c.start()
    play(c, video, playlist, **extra)
    assert remote.adds == []


def test_stale_playback_is_ignored(poc):
    c, remote, *_ = poc
    c.start()
    c.ctx.bridge.now_playing = {"video_id": "v3", "playlist_ytm_id": "PLSESSION"}
    c.listening.observe(c.session)
    c.session.browser_seen_at = time.monotonic() - 20
    c.advance(c.session)
    assert remote.adds == []


@pytest.mark.parametrize("presence", ["missing", "paused", "stale", "disconnected", "missing_playlist"])
def test_established_session_survives_temporary_playback_gaps(poc, presence):
    c, remote, frames, workers, _ = poc
    c.start()
    play(c, "v1")
    assert c.active
    bridge = c.ctx.bridge
    if presence == "missing":
        bridge.now_playing = None
    elif presence == "paused":
        bridge.now_playing["paused"] = True
    elif presence == "stale":
        bridge.now_playing_seen_at = time.monotonic() - 20
    elif presence == "disconnected":
        bridge.connected = False
    else:
        bridge.now_playing.pop("playlist_ytm_id")
    state = c.status()
    assert state["active"] and not state["starting"]
    assert state["playlist_id"] == "PLSESSION"
    assert len(frames) == len(workers) == 1
    assert remote.adds == []
    assert not c.stop()["active"]


def test_fresh_playback_elsewhere_leaves_radio_even_if_later_reports_are_missing(poc):
    c, *_ = poc
    c.start()
    play(c, "v1")
    assert c.active
    play(c, "elsewhere", "OTHER")
    assert not c.active
    c.ctx.bridge.now_playing = None
    assert not c.active
    play(c, "v1")
    assert c.active


def test_launch_waits_for_playback_and_expires_without_confirming_a_session(poc):
    c, *_ = poc
    state = c.start()
    assert state["active"] and state["starting"]
    c.ctx.bridge.now_playing = {"video_id": "v1", "playlist_ytm_id": "PLSESSION", "paused": True}
    c.ctx.bridge.now_playing_seen_at = time.monotonic()
    assert c.status()["starting"]
    c.session.launch_deadline = time.monotonic() - 1
    assert not c.status()["active"]
    assert not c.status()["starting"]
    assert not c.session.playback_confirmed


@pytest.mark.parametrize("count", [0, 2, 4])
def test_launch_from_paused_tab_accepts_exact_tracks_with_stale_header_count(poc, monkeypatch, count):
    c, remote, frames, *_ = poc
    c.ctx.bridge.now_playing = {"video_id": "old-song", "playlist_ytm_id": "OLD", "paused": True}
    c.ctx.bridge.now_playing_seen_at = time.monotonic()
    read = remote.get_playlist
    monkeypatch.setattr(remote, "get_playlist", lambda *args, **kwargs:
                        {**read(*args, **kwargs), "trackCount": count})
    state = c.start("v5")
    assert state["ok"] and state["starting"]
    assert c.session.initial_tracks == []
    assert [t["video_id"] for t in state["tracks"]] == ["v5", "v1", "v2"]
    assert frames == [{"type": "navigate", "url":
                       "https://music.youtube.com/watch?v=v5&list=PLSESSION"}]
    # Later edits still require a complete snapshot with a consistent count.
    with pytest.raises(PlaylistPending):
        c._remote_tracks(c.session)
    play(c, "v5")
    assert not c.status()["starting"]


@pytest.mark.parametrize("detail", [
    {"tracks": [{"videoId": "v1"}, {"videoId": "v2"}], "trackCount": 3},
    {"tracks": [{"videoId": "v2"}, {"videoId": "v1"}, {"videoId": "v3"}], "trackCount": 3},
    {"tracks": [{"videoId": "v1"}, {"videoId": "v2"}, {"videoId": "v3"}], "continuation": "next"},
    {"tracks": [{"videoId": "v1"}, {"videoId": "v2"}, {"videoId": "v3"}], "error": "not ready"},
])
def test_launch_does_not_navigate_for_incomplete_or_different_contents(poc, monkeypatch, detail):
    c, remote, frames, *_ = poc
    monkeypatch.setattr(remote, "get_playlist", lambda *args, **kwargs: detail)
    state = c.start()
    assert state["ok"] and state["starting"]
    assert frames == []
    assert c.session.initial_tracks


def test_playlist_preparation_times_out_without_leaving_an_endless_launch(poc, monkeypatch):
    c, remote, frames, *_ = poc
    monkeypatch.setattr(remote, "get_playlist", lambda *args, **kwargs: {"tracks": [], "trackCount": 0})
    assert c.start()["starting"]
    s = c.session
    s.preparation_deadline = time.monotonic() - 1
    with pytest.raises(PlaylistChanged, match="preparation timed out"):
        c._complete_start(s)
    monkeypatch.setattr(s.wake, "wait", lambda _: False)
    c._monitor(s)
    assert not c.status()["active"]
    assert "preparation timed out" in c.status()["error"]
    assert frames == []


def test_restart_with_old_radio_paused_starts_the_chosen_song(poc):
    c, remote, frames, *_ = poc
    c.start()
    play(c, "v1")
    c.suspend()
    c.ctx.bridge.now_playing["paused"] = True
    restarted = PlaylistRadio(c.ctx)
    state = restarted.start("v5")
    assert state["ok"] and not state.get("joined")
    assert remote.ids == ["v5", "v1", "v2"]
    assert frames[-1]["url"] == "https://music.youtube.com/watch?v=v5&list=PLSESSION"
    assert state["starting"]
    restarted.stop()
    checkpoint = restarted.persistence.load()
    with pytest.raises(ValueError, match='changed.*worker'):
        c.stop()
    c.suspend()
    assert restarted.persistence.load() == checkpoint


def test_native_observer_confirms_launch_without_a_visible_radio_page(poc):
    c, *_ = poc
    c.start()
    play(c, "v1")
    assert c.session.playback_confirmed
    c.ctx.bridge.now_playing = None
    c.session.launch_deadline = time.monotonic() - 1
    assert c.status()["active"]
    assert not c.status()["starting"]


def test_seed_outside_prepared_pool_resolves_exact_library_upload(poc):
    c, remote, *_ = poc
    c.ctx.store.tracks.upsert_track("v40", "Outside", "Artist", "", 200)
    result = c.start("v40")
    assert result["ok"]
    assert remote.ids[0] == "v40"


def test_expired_seed_falls_back_to_current_offer(poc):
    c, remote, *_ = poc
    result = c.start("expired-upload")
    assert result["ok"]
    assert remote.ids == ["v1", "v2", "v3"]
def test_stop_preserves_playlist_and_prevents_future_appends(poc):
    c, remote, frames, *_ = poc
    c.start()
    assert c.stop()["ok"]
    play(c, "v2")
    assert remote.ids == ["v1", "v2", "v3"]
    assert len(frames) == 1
    assert not c.status()["active"]


def test_start_does_not_replace_running_session(poc):
    c, remote, _, workers, _ = poc
    assert c.start()["ok"]
    again = c.start()
    assert again["ok"] and again["joined"]
    assert len(workers) == 1
    assert remote.ids == ["v1", "v2", "v3"]


def test_start_requires_three_recommendations_before_creating_playlist(poc):
    c, remote, frames, workers, pool = poc
    del pool[2:]
    assert not c.start()["ok"]
    assert remote.ids == []
    assert frames == workers == []


@pytest.mark.parametrize("unavailable", ["extension", "client"])
def test_start_requires_connection_and_an_idle_existing_radio(poc, unavailable):
    c, remote, frames, workers, _ = poc
    if unavailable == "extension":
        c.ctx.bridge.connected = False
    elif unavailable == "client":
        c.ctx.client_provider = lambda: {}
    assert not c.start()["ok"]
    assert remote.ids == []
    assert frames == workers == []


def test_exhausted_catalog_does_not_repeat_songs(poc):
    c, remote, _, _, pool = poc
    del pool[3:]
    c.start()
    play(c, "v3")
    assert c.status()["exhausted"]
    assert remote.adds == []


def test_stop_during_start_prevents_navigation_and_monitor(poc, monkeypatch):
    c, remote, frames, workers, _ = poc
    create = playlist_generated.create_generated_playlist

    def stop_during_create(*args):
        result = create(*args)
        c.stop()
        return result

    monkeypatch.setattr(playlist_generated, "create_generated_playlist", stop_during_create)
    assert not c.start()["ok"]
    assert remote.ids == ["v1", "v2", "v3"]
    assert frames == workers == []


@pytest.mark.parametrize("disconnected", [False, True])
def test_monitor_exits_when_application_closes_or_extension_disconnects(poc, disconnected):
    c, *_ = poc
    c.start()
    play(c, "v1")
    assert c.active  # End the explicit-start navigation grace before disconnecting.
    if disconnected:
        c.ctx.bridge.connected = False
    else:
        c.work.close_admission()
    # Real event with an immediate first observation keeps the shutdown test fast.
    class ImmediateEvent(threading.Event):
        def wait(self, timeout=None):
            return self.is_set()
    c.session.wake = ImmediateEvent()
    c._monitor(c.session)
    assert c.session.stop.is_set()
    assert not c.session.monitoring
    if disconnected:
        assert c.status()["active"]  # Keep the page attached while the extension reconnects.
        assert not c.status()["connected"]


@pytest.mark.parametrize('stop', ['admission', 'session', 'disconnect', None])
def test_observation_send_disconnect_after_connected_check(poc, caplog, stop):
    c, remote, *_ = poc
    assert c.start()['ok']
    play(c, 'v1')
    s = c.session
    before = (list(remote.ids), list(remote.adds), s.observed_video, s.phase)
    sends = []

    class BoundedWake(threading.Event):
        def wait(self, timeout=None):
            if sends:
                s.stop.set()
            return True

    s.wake = BoundedWake()

    async def scenario():
        bridge = Bridge()
        bridge.now_playing = c.ctx.bridge.now_playing
        bridge.now_playing_seen_at = c.ctx.bridge.now_playing_seen_at
        c.ctx.bridge = c.playback.bridge = bridge

        async def receive():
            return {'type': 'websocket.connect'}

        async def send(message):
            if message['type'] != 'websocket.send':
                return
            assert bridge.connected and c.work.accepting and not s.stop.is_set()
            sends.append(json.loads(message['text']))
            # The real Bridge request and Starlette send have both started.
            if stop == 'admission':
                c.work.close_admission()
                assert not s.stop.is_set()
            elif stop == 'session':
                s.stop.set()
            elif stop == 'disconnect':
                bridge.disconnect(connection)
            raise OSError('Controlled socket closure during observation send')

        socket = WebSocket({'type': 'websocket'}, receive, send)
        await socket.accept()
        connection = bridge.connect(socket.send_json, asyncio.get_running_loop())
        try:
            await asyncio.wait_for(asyncio.to_thread(c._monitor, s), 2)
            assert not bridge._pending
            assert socket.application_state == WebSocketState.DISCONNECTED
        finally:
            s.stop.set()
            bridge.disconnect(connection)

    with caplog.at_level(logging.WARNING, logger='yt_playlist.web.radio_session'):
        asyncio.run(scenario())
    assert len(sends) == 1 and sends[0]['type'] == 'radio-session'
    assert (remote.ids, remote.adds, s.observed_video, s.phase) == before
    assert s.stop.is_set() and not s.monitoring
    saved = c.persistence.load()
    assert saved['episode_id'] == s.episode_id and saved['phase'] == s.phase
    assert saved['tracks'] == s.tracks and not saved['stopped']
    failures = [record for record in caplog.records
                if record.name == 'yt_playlist.web.radio_session' and record.levelno >= logging.WARNING]
    if stop is None:
        failure, = failures
        assert failure.message == 'Radio Session maintenance failed'
        assert failure.exc_info and failure.exc_info[2]
    else:
        assert not failures
        assert s.error is None


@pytest.mark.parametrize('closing', [False, True])
@pytest.mark.parametrize('failure_type', [RuntimeError, WebSocketDisconnect])
def test_append_readback_failure_still_warns_and_recovers_without_replay(
        poc, monkeypatch, caplog, closing, failure_type):
    c, remote, *_ = poc
    assert c.start()['ok']
    play(c, 'v1')
    c.ctx.bridge.now_playing['video_id'] = 'v3'
    c.listening.observe(c.session)

    read = remote.get_playlist

    def failed_read(*args, **kwargs):
        monkeypatch.setattr(remote, 'get_playlist', read)
        raise (WebSocketDisconnect(1006) if failure_type is WebSocketDisconnect
               else RuntimeError('Read response lost'))

    def lose_readback():
        monkeypatch.setattr(remote, 'get_playlist', failed_read)
        if closing:
            c.work.close_admission()

    remote.after_add = lose_readback

    class BoundedWake(threading.Event):
        def wait(self, timeout=None):
            if remote.adds:
                c.session.stop.set()
            return True

    c.session.wake = BoundedWake()
    with caplog.at_level(logging.WARNING, logger='yt_playlist.web.radio_session'):
        c._monitor(c.session)
    failure, = [record for record in caplog.records
                if record.message == 'Radio Session maintenance failed']
    assert isinstance(failure.exc_info[1], failure_type)
    assert failure.exc_info[2]
    assert remote.ids == ['v1', 'v2', 'v3', 'v4'] and remote.adds == [['v4']]
    saved = c.persistence.load()
    assert saved['pending_sent'] and saved['pending']['video_id'] == 'v4'
    assert not saved['stopped']

    remote.after_add = lambda: None
    if closing:
        c.work.open()
    restored = PlaylistRadio(c.ctx)
    try:
        assert restored.join()['active']
        play(restored, 'v4')
        assert remote.ids == ['v1', 'v2', 'v3', 'v4', 'v5']
        assert remote.adds == [['v4'], ['v5']]
        assert not restored.persistence.load()['pending_sent']
    finally:
        restored.suspend()
