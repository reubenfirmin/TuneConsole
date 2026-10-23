import json
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from yt_playlist.library import executor
from yt_playlist.web import radio_session
from yt_playlist.web.radio_session import PlaylistRadio
from yt_playlist.web.radio_playlist_io import PlaylistChanged, PlaylistPending
from yt_playlist.web.routes import radio_session as radio_routes


class Playlist:
    def __init__(self):
        self.ids = []
        self.adds = []
        self.removes = []
        self.fail_add = False
        self.lose_response = False
        self.fail_read = False
        self.after_add = lambda: None

    def get_playlist(self, playlist, limit=None):
        assert playlist == "PLSESSION"
        if self.fail_read:
            self.fail_read = False
            raise RuntimeError("Read response lost")
        return {"tracks": [{"videoId": vid, "setVideoId": "set-" + vid} for vid in self.ids],
                "trackCount": len(self.ids)}

    def add_playlist_items(self, playlist, ids):
        assert playlist == "PLSESSION"
        self.adds.append(list(ids))
        if self.fail_add:
            return {"status": "STATUS_FAILED"}
        self.ids.extend(ids)
        self.after_add()
        if self.lose_response:
            raise RuntimeError("Add response lost")
        return {"status": "STATUS_SUCCEEDED"}

    def remove_playlist_items(self, playlist, tracks):
        assert playlist == "PLSESSION"
        ids = [t["videoId"] for t in tracks]
        self.removes.append(ids)
        for video in ids:
            self.ids.remove(video)
        return {"status": "STATUS_SUCCEEDED"}


@pytest.fixture
def poc(monkeypatch, store):
    remote, frames, workers = Playlist(), [], []
    identity = store.identities.upsert_identity("main", "ref", None, True)
    bridge = SimpleNamespace(connected=True, now_playing=None, now_playing_seen_at=None,
                             send_control=frames.append, requests=[], native_ids=[], auto_adopt=True)

    async def request_control(frame):
        bridge.requests.append(frame)
        player = dict(bridge.now_playing or {})
        if frame["refresh"] and bridge.auto_adopt:
            bridge.native_ids = list(remote.ids)
        video = player.get("video_id")
        expected = frame["videoIds"]
        native = bridge.native_ids
        synced = (player.get("playlist_ytm_id") == frame["playlist"]
                  and video in expected and video in native
                  and expected[expected.index(video) + 1:] == native[native.index(video) + 1:])
        return 200, json.dumps({"ok": True, "player": player, "playback_synced": synced,
            "native": {"available": True, "videoIds": native},
            "refresh": "requested" if frame["refresh"] else "not requested"})

    bridge.request_control = request_control
    ctx = SimpleNamespace(bridge=bridge, store=store,
                          now_fn=lambda: 1234.0, client_provider=lambda: {identity: remote})
    controller = PlaylistRadio(ctx)
    ctx.radio = controller
    monkeypatch.setattr(controller.work, "start_thread", lambda *args, **kw: workers.append((args, kw)))
    pool = [{"key": f"k{i}", "video_id": f"v{i}", "title": f"Song {i}", "artist": f"Artist {i}"}
            for i in range(1, 33)]
    for track in pool:
        tid = store.tracks.upsert_track(track["video_id"], track["title"], track["artist"], "", None)
        track["key"] = store.tracks.catalog_subject(tid)["identity_key"]

    class Candidates:
        def __init__(self, store, now, tuning=None, *, now_source=None):
            self.tuning = radio_session.radio_tuning.controls(tuning or radio_session.radio_tuning.DEFAULTS)
            self.now_sampled_at = now
            self.encoding_model = store.settings.get_setting("rec_content_model")
            self.now_posterior, self.now_gain = {}, 0.
            self.by_video = {t["video_id"]: t for t in pool}
            self.by_key = {t["key"]: t for t in pool}
            self.fingerprint = "fixed-test-candidates"
            self.modes = {}

        def refresh_now(self, now):
            self.now_sampled_at = now

        def refresh_metadata(self, tracks):
            return False

        def fits(self, track, anchor=None):
            return True

        def retain(self, tracks):
            self.by_video.update((t["video_id"], t) for t in tracks)
            self.by_key.update((t["key"], t) for t in tracks)

        def cluster_for(self, track):
            return int(track["video_id"][1:]) // 8

        def similarity(self, a, b):
            return float(a["key"] == b["key"])

        def seed_offer(self, n=16):
            return pool[:n]

        def next_track(self, history, *, target=None, feedback=None, excluded=(), current=None):
            used = {t["video_id"] for t in history} | set(excluded)
            ordered = list(reversed(pool)) if self.tuning["comfort"] < .5 else pool
            return next((t for t in ordered if t["video_id"] not in used), None)

        def direction_anchor(self, history, anchors, *, excluded=(), feedback=None):
            return self.next_track(history + anchors, excluded=excluded)

    monkeypatch.setattr(radio_session, "RadioCandidates", Candidates)

    def create(store, title, tracks, client, now, identity):
        assert title.startswith("Radio Session · ")
        assert identity == store.identities.get_master_identity().id
        remote.ids = [t["video_id"] for t in tracks]
        bridge.native_ids = list(remote.ids)
        pid = store.playlists.upsert_playlist(identity, "PLSESSION", title, len(tracks), "", now)
        track_ids = store.tracks.track_ids_for_videos(remote.ids)
        store.playlists.set_playlist_tracks(pid, [track_ids[video] for video in remote.ids])
        return {"new_ytm": "PLSESSION", "pid": pid, "added": len(tracks)}

    monkeypatch.setattr(executor, "create_generated_playlist", create)
    yield controller, remote, frames, workers, pool
    controller.stop()
    controller.work.stop_executor()


def play(controller, video, playlist="PLSESSION", **extra):
    controller.ctx.bridge.now_playing = {"video_id": video, "playlist_ytm_id": playlist,
                                       "paused": False, "position": 30, "duration": 300, **extra}
    controller.ctx.bridge.now_playing_seen_at = time.monotonic()
    controller._browser(controller.session)
    controller.advance(controller.session)


@pytest.mark.parametrize("prior_titles", [
    [],
    ["Radio Session · 2026-10-22"],
    ["Radio Session · 2026-10-22 #1", "Radio Session · 2026-10-22 #2"],
])
def test_radio_session_titles_are_numbered_per_local_day(poc, prior_titles):
    c, *_ = poc
    store = c.ctx.store
    identity = store.identities.get_master_identity().id
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


def test_three_to_start_then_one_append_per_song_and_no_playback_queue_commands(poc):
    c, remote, frames, workers, _ = poc
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
    c._browser(c.session)
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
    monkeypatch.setattr(s.stop, "wait", lambda _: False)
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


def test_native_observer_confirms_launch_without_a_visible_radio_page(poc):
    c, *_ = poc
    c.start()
    play(c, "v1")
    assert c.session.playback_confirmed
    c.ctx.bridge.now_playing = None
    c.session.launch_deadline = time.monotonic() - 1
    assert c.status()["active"]
    assert not c.status()["starting"]


def test_append_uses_native_observation_instead_of_stale_bridge_video(poc):
    c, remote, *_ = poc
    c.start()
    play(c, "v1")
    c.ctx.bridge.now_playing["video_id"] = "v3"
    c.advance(c.session)
    assert remote.adds == []
    c._browser(c.session)
    c.advance(c.session)
    assert remote.adds == [["v4"]]


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
    c._browser(c.session)
    assert not c.session.refresh_needed
    c._browser(c.session)
    assert not bridge.requests[-1]["refresh"]
    bridge.native_ids = ["v1", "v2", "v3", "old-next"]
    c._browser(c.session)
    assert c.session.refresh_needed
    c._browser(c.session)
    assert bridge.requests[-1]["refresh"]
    assert remote.adds == [["v4"]]


def test_latest_direction_choice_updates_only_future_and_waits_for_native_ack(poc):
    c, remote, frames, *_ = poc
    c.start()
    play(c, "v2")
    bridge = c.ctx.bridge
    bridge.auto_adopt = False
    bridge.requests.clear()
    state = c.status()
    first, latest = state["directions"][1:3]
    for choice in (first, latest):
        selected = c.select(choice["id"], state["menu_id"])
        assert selected["ok"] and selected["updating"]
        assert selected["pending_slot"] == choice["slot"]
    assert remote.ids == ["v1", "v2", "v3"]
    assert remote.adds == remote.removes == []
    assert bridge.requests == []

    c.advance(c.session)
    desired = ["v1", "v2", latest["tracks"][0]["video_id"]]
    assert remote.ids == desired
    assert remote.adds == [[desired[-1]]]
    assert remote.removes == [["v3"]]
    assert c.status()["updating"]
    assert c.session.directions.active == 0
    assert all(frame["videoIds"] == desired for frame in bridge.requests if frame["refresh"])
    assert any(frame["refresh"] for frame in bridge.requests)
    bridge.native_ids = list(desired)
    c.advance(c.session)
    assert not c.status()["updating"]
    assert c.session.directions.active == latest["slot"]
    assert len(frames) == 1
    assert c.status()["history_count"] == 2
    assert bridge.now_playing["video_id"] == "v2"
    assert bridge.now_playing["position"] == 30


@pytest.mark.parametrize("failure", ["rejected", "lost_response"])
def test_direction_switch_skips_an_unavailable_track_and_preserves_played_history(poc, failure):
    c, remote, *_ = poc
    clock = [1234.]
    c.ctx.now_fn = lambda: clock[0]
    c.start()
    play(c, "v2")
    state = c.status()
    branch = state["directions"][2]
    unavailable = branch["tracks"][0]["video_id"]
    following = branch["tracks"][1]["video_id"]
    add = remote.add_playlist_items
    def fail_one(playlist, ids):
        if ids == [unavailable]:
            remote.adds.append(list(ids))
            if failure == "lost_response":
                raise RuntimeError("Response lost")
            return {"status": "STATUS_FAILED"}
        return add(playlist, ids)
    remote.add_playlist_items = fail_one
    assert c.select(branch["id"], state["menu_id"])["ok"]
    c.advance(c.session)
    if failure == "lost_response":
        assert c.status()["updating"]
        clock[0] += 31
        c.advance(c.session)
    assert remote.ids == ["v1", "v2", following]
    assert remote.adds == [[unavailable], [following]]
    assert remote.removes == [["v3"]]
    assert c.session.replacement is None
    assert c.session.directions.active == 2
    assert unavailable in c.session.rejected
    assert c.session.directions.feedback == {}
    assert c.ctx.bridge.now_playing["video_id"] == "v2"


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
def test_saved_session_keeps_loading_only_for_confirmed_playback_until_join(poc, presence):
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
    assert before["resolving"] is (presence == "matching")
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
        c._browser(c.session)
    assert commands == ["radio-session"]
    assert c.status()["observer_error"] == "disconnected"


def test_lost_add_response_is_read_back_without_duplicate_append(poc):
    c, remote, *_ = poc
    c.start()
    remote.lose_response = True
    play(c, "v3")
    play(c, "v3")
    assert remote.ids == ["v1", "v2", "v3", "v4"]
    assert remote.adds == [["v4"]]
    assert c.status()["ahead"] == 1


def test_uncertain_read_after_append_is_recovered_before_another_write(poc):
    c, remote, *_ = poc
    c.start()
    remote.after_add = lambda: setattr(remote, "fail_read", True)
    with pytest.raises(RuntimeError, match="Read response lost"):
        play(c, "v3")
    assert c.status()["ahead"] == 0
    assert c.session.pending["video_id"] == "v4"
    play(c, "v3")
    assert remote.adds == [["v4"]]
    assert c.status()["ahead"] == 1


def test_rejected_append_skips_the_candidate_and_adds_the_next_track(poc):
    c, remote, *_ = poc
    c.start()
    add = remote.add_playlist_items
    def reject_first(playlist, ids):
        remote.fail_add = ids == ["v4"]
        return add(playlist, ids)
    remote.add_playlist_items = reject_first
    play(c, "v3")
    assert remote.ids == ["v1", "v2", "v3", "v5"]
    assert remote.adds == [["v4"], ["v5"]]
    assert c.session.pending is None
    assert "v4" in c.session.rejected
    assert c.session.directions.feedback == {}, "delivery failure is not a taste signal"
    assert c.status()["ahead"] == 1


@pytest.mark.parametrize("reply", ["success_without_row", "lost_reply"])
def test_unconfirmed_append_has_a_bounded_wait_then_skips_without_repeating_the_write(poc, reply):
    c, remote, *_ = poc
    clock = [1234.]
    c.ctx.now_fn = lambda: clock[0]
    c.start()
    add = remote.add_playlist_items
    def unconfirmed_first(playlist, ids):
        if ids == ["v4"]:
            remote.adds.append(list(ids))
            if reply == "lost_reply":
                raise RuntimeError("Response lost")
            return {"status": "STATUS_SUCCEEDED"}
        return add(playlist, ids)
    remote.add_playlist_items = unconfirmed_first
    play(c, "v3")
    clock[0] += 29
    play(c, "v3")
    assert remote.adds == [["v4"]]
    clock[0] += 2
    play(c, "v3")
    assert remote.ids == ["v1", "v2", "v3", "v5"]
    assert remote.adds == [["v4"], ["v5"]]
    assert c.session.pending is None and not c.session.halted


@pytest.mark.parametrize("lost_read", [False, True])
def test_acknowledged_upload_substitution_is_confirmed_without_merging_recordings(poc, lost_read):
    c, remote, *_ = poc
    c.start()
    original_key = c.session.candidates.by_video["v4"]["key"]
    def substitute(playlist, ids):
        assert ids == ["v4"]
        remote.adds.append(list(ids))
        remote.ids.append("v31")
        remote.fail_read = lost_read
        return {"status": "STATUS_SUCCEEDED", "playlistEditResults": [
            {"videoId": "v31", "setVideoId": "set-v31"}]}
    remote.add_playlist_items = substitute
    if lost_read:
        with pytest.raises(RuntimeError, match="Read response lost"):
            play(c, "v3")
        saved = json.loads(c.ctx.store.settings.get_setting("radio_current_session"))
        assert saved["pending_result"]["videoId"] == "v31"
        restored = PlaylistRadio(c.ctx)
        restored.join()
        c = restored
    play(c, "v3")
    assert remote.adds == [["v4"]]
    assert c.status()["tracks"][-1]["video_id"] == "v31"
    assert c.status()["tracks"][-1]["key"] != original_key
    assert c.session.pending is None and c.status()["ahead"] == 1
    assert "v4" in c.session.rejected
    assert c.status()["error"] is None


def test_an_unacknowledged_different_tail_is_not_claimed_as_our_substitution(poc):
    c, remote, *_ = poc
    c.start()
    def unrelated_tail(playlist, ids):
        remote.adds.append(list(ids))
        remote.ids.append("v31")
        return {"status": "STATUS_SUCCEEDED", "playlistEditResults": [
            {"videoId": "v31", "setVideoId": "different-handle"}]}
    remote.add_playlist_items = unrelated_tail
    with pytest.raises(PlaylistPending):
        play(c, "v3")
    assert [t["video_id"] for t in c.status()["tracks"]] == ["v1", "v2", "v3"]
    assert remote.ids == ["v1", "v2", "v3", "v31"]
    assert not remote.removes


@pytest.mark.parametrize("lost_read", [False, True])
def test_direction_replacement_recovers_an_acknowledged_substitution(poc, lost_read):
    from yt_playlist.web.radio_playlist_tail import RadioPlaylistTail

    c, remote, *_ = poc
    c.start()
    requested = c.session.candidates.by_video["v4"]
    c.session.edits.begin(RadioPlaylistTail(c.session.tracks, [requested], 0))

    def substitute(playlist, ids):
        assert ids == ["v4"]
        remote.adds.append(list(ids))
        remote.ids.append("v31")
        remote.fail_read = lost_read
        return {"status": "STATUS_SUCCEEDED", "playlistEditResults": [
            {"videoId": "v31", "setVideoId": "set-v31"}]}

    remote.add_playlist_items = substitute
    if lost_read:
        with pytest.raises(RuntimeError, match="Read response lost"):
            play(c, "v3")
        saved = json.loads(c.ctx.store.settings.get_setting("radio_current_session"))
        assert saved["replacement"]["receipt"]["videoId"] == "v31"
        restored = PlaylistRadio(c.ctx)
        restored.join()
        c = restored
    play(c, "v3")
    assert remote.adds == [["v4"]]
    assert c.session.replacement is None
    assert c.status()["tracks"][-1]["video_id"] == "v31"
    assert c.status()["ahead"] == 1
    assert "v4" in c.session.rejected
    assert c.status()["error"] is None


def test_playing_song_moves_into_history_even_while_radio_is_halted(poc):
    from yt_playlist.web.radio_state import Phase

    c, remote, *_ = poc
    c.start()
    play(c, "v1")
    c.session.move(Phase.CONFLICT)
    c.ctx.bridge.now_playing = {"video_id": "v3", "playlist_ytm_id": "PLSESSION", "paused": False}
    c.ctx.bridge.now_playing_seen_at = time.monotonic()
    c._browser(c.session)
    assert c.status()["position"] == 2
    assert c.status()["history_count"] == 3
    assert c.session.halted
    assert not remote.adds
    assert all(t["video_id"] != "v3" for branch in c.status()["directions"] for t in branch["tracks"])


@pytest.mark.parametrize("kind", ["equivalent", "remix", "missing_duration"])
def test_lost_substitution_reply_requires_complete_matching_recording_metadata(poc, kind):
    c, remote, *_ = poc
    c.start()
    original = c.session.candidates.by_video["v4"]
    original["duration"] = 239
    read = remote.get_playlist
    def metadata(playlist, limit=None):
        detail = read(playlist, limit)
        for row in detail["tracks"]:
            if row["videoId"] == "v31":
                row.update(title=original["title"] + (" (Remix)" if kind == "remix" else ""),
                           artists=[{"name": original["artist"]}])
                if kind != "missing_duration":
                    row["duration_seconds"] = 239
        return detail
    remote.get_playlist = metadata
    def substitute(playlist, ids):
        remote.adds.append(list(ids))
        remote.ids.append("v31")
        raise RuntimeError("Lost response")
    remote.add_playlist_items = substitute
    if kind == "equivalent":
        play(c, "v3")
        assert c.status()["tracks"][-1]["video_id"] == "v31"
        assert c.status()["tracks"][-1]["key"] != original["key"]
        assert c.session.pending is None
        assert c.status()["ahead"] == 1
    else:
        with pytest.raises(PlaylistPending):
            play(c, "v3")
        assert c.session.pending["video_id"] == "v4"
    assert remote.adds == [["v4"]] and not remote.removes


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


def test_stop_during_append_prevents_the_next_addition(poc):
    c, remote, *_ = poc
    c.start()
    remote.after_add = c.stop
    play(c, "v3")
    assert remote.ids == ["v1", "v2", "v3", "v4"]
    assert not c.status()["active"]


def test_external_edits_pause_writes_and_preserve_confirmed_radio_view(poc, monkeypatch):
    c, remote, *_ = poc
    c.start()
    play(c, "v1")
    remote.ids.append("manual-song")
    c.ctx.bridge.now_playing = {"video_id": "v3", "playlist_ytm_id": "PLSESSION"}
    tick = [100.]
    class ImmediateEvent(threading.Event):
        calls = 0
        def wait(self, timeout=None):
            self.calls += 1
            tick[0] += 40
            return self.calls > 3 or self.is_set()
    monkeypatch.setattr(radio_session.time, "monotonic", lambda: tick[0])
    c.session.stop = ImmediateEvent()
    c._monitor(c.session)
    assert c.status()["active"]
    assert c.session.halted
    assert "changed elsewhere" in c.status()["error"]
    c._browser(c.session)
    assert "changed elsewhere" in c.status()["error"], "a healthy observer must not hide a halted playlist"
    assert remote.ids == ["v1", "v2", "v3", "manual-song"]
    assert remote.adds == []


def test_transient_wrong_playlist_context_on_owned_song_keeps_radio(poc):
    c, *_ = poc
    c.start()
    play(c, "v1")
    c.ctx.bridge.now_playing["playlist_ytm_id"] = "PREVIOUS"
    assert c.status()["active"]
    c.ctx.bridge.now_playing["video_id"] = "another-song"
    assert not c.status()["active"]


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


def test_tuning_preserves_history_and_waits_for_native_future_ack(poc):
    c, remote, frames, *_ = poc
    c.start()
    play(c, "v2")
    s, bridge = c.session, c.ctx.bridge
    bridge.auto_adopt = False
    history = list(s.heard)
    choices = dict(s.choices)
    controls = {"comfort": .1, "theme": .9, "energy": .2}
    result = c.tune(controls)
    assert result["ok"] and result["updating"]
    assert s.candidates is s.directions.candidates
    assert result["tuning"] == controls
    assert [b["decay_remaining"] for b in result["directions"] if not b["active"]] == [5, 4, 3]
    assert s.heard == history and s.choices == choices
    assert remote.ids == ["v1", "v2", "v3"]
    assert remote.adds == remote.removes == []
    requested = s.requested_direction["tracks"][0]["video_id"]
    c.advance(s)
    assert remote.ids == ["v1", "v2", requested]
    assert c.status()["updating"]
    assert bridge.now_playing["video_id"] == "v2"
    bridge.native_ids = list(remote.ids)
    c.advance(s)
    assert not c.status()["updating"]
    assert s.heard == history
    assert requested not in s.choices  # Slider changes aren't explicit song choices.
    assert len(frames) == 1
    saved = json.loads(c.ctx.store.settings.get_setting("radio_current_session"))
    assert saved["tuning"] == controls
    assert len(set(saved["directions"]["decay"].values())) == 3


@pytest.mark.parametrize("observe_before_publish", [False, True])
@pytest.mark.parametrize("retune", [False, True])
def test_native_transition_during_tail_removal_stays_in_history(poc, monkeypatch, observe_before_publish, retune):
    c, remote, frames, *_ = poc
    c.start()
    play(c, "v1")
    if retune:
        c.tune({"comfort": .1, "theme": .9, "energy": .2})
    else:
        branch = c.status()["directions"][1]
        c.select(branch["id"], c.status()["menu_id"])
    target = c.session.requested_direction["tracks"][0]["video_id"]
    remove = remote.remove_playlist_items

    def transition(playlist, tracks):
        assert tracks[0]["videoId"] == "v2"
        c.ctx.bridge.now_playing["video_id"] = "v2"
        if observe_before_publish:
            c._browser(c.session)
        return remove(playlist, tracks)

    monkeypatch.setattr(remote, "remove_playlist_items", transition)
    c.advance(c.session)
    state = c.status()
    assert remote.ids == ["v1", "v3", target]
    assert [t["video_id"] for t in state["tracks"]] == remote.ids
    assert [t["video_id"] for t in state["history"]] == ["v1", "v2"]
    assert state["history_count"] == 2
    assert state["position"] == -1 and c.session.furthest == 0
    assert state["browser"]["player"]["video_id"] == "v2"
    assert "v2" in c.session.directions.retired
    assert all(t["video_id"] != "v2" for b in state["directions"] for t in b["tracks"])
    assert PlaylistRadio(c.ctx).bootstrap()["state"]["history"] == state["history"]

    # The history endpoint must load metadata by playback identity, even though
    # v2 no longer has membership in the saved playlist.
    c.ctx.templates = Jinja2Templates(directory="src/yt_playlist/web/templates")
    app = FastAPI()
    app.include_router(radio_routes.build(c.ctx))
    with TestClient(app) as client:
        html = client.get("/radio/playlist").text
    assert 'data-vid="v2"' in html and "Song 2" in html
    assert 'data-vid="v3"' not in html
    assert f'data-vid="{target}"' not in html

    play(c, "v3")
    state = c.status()
    assert not state["updating"]
    assert state["browser"]["playback_synced"]
    assert [t["video_id"] for t in state["history"]] == ["v1", "v2", "v3"]
    assert state["ahead"] == 1
    assert len(frames) == 1


def test_tuning_honors_a_pending_explicit_stream_selection(poc):
    c, *_ = poc
    c.start()
    play(c, "v2")
    branch = c.status()["directions"][2]
    c.select(branch["id"], c.status()["menu_id"])
    choice = dict(c.session.requested_direction["choice"])
    result = c.tune({"comfort": .1, "theme": .9, "energy": .2})
    assert result["ok"] and result["pending_slot"] == 2
    assert [b["decay_remaining"] for b in result["directions"] if not b["active"]] == [5, 4, 3]
    assert c.session.requested_direction["choice"] == choice
    c.advance(c.session)
    assert c.session.directions.active == 2
    assert c.ctx.bridge.now_playing["video_id"] == "v2"


@pytest.mark.parametrize("retuned", [False, True])
def test_bootstrap_preserves_staggered_decay_progress(poc, retuned):
    c, *_ = poc
    c.start()
    play(c, "v2")
    if retuned:
        c.tune({"comfort": .1, "theme": .9, "energy": .2})
    c._persist(c.session)
    current = c.status()["directions"]
    restored = PlaylistRadio(c.ctx).bootstrap()["state"]["directions"]
    assert [(b["decay_remaining"], b["decay_total"]) for b in restored] == [
        (b["decay_remaining"], b["decay_total"]) for b in current]
    assert [b["decay_remaining"] for b in restored if not b["active"]] == [5, 4, 3]


def prepare_radio_styles(controller, pool, monkeypatch):
    """Publish four real prepared regions after a session has already started cold."""
    import numpy as np
    from yt_playlist.rec import embed
    from yt_playlist.rec.radio_candidates import RadioCandidates
    from tests.model_support import build_content_model

    store = controller.ctx.store
    ids = store.tracks.track_ids_for_videos([t["video_id"] for t in pool])
    bundles, modes = {}, []
    genres = ("Ambient", "Techno", "Jazz", "Rock")
    for index, track in enumerate(pool):
        store.tracks.set_track_enrichment(ids[track["video_id"]], genres[index // 8], "2001")
    build_content_model(store)
    keys = [pool[index * 8]["key"] for index in range(4)]
    vectors = store.rec_model.content_vectors_by_keys(keys)
    for index, genre in enumerate(genres):
        mid = index + 1
        tracks = [{**t, "genre": genre, "year": "2001"} for t in pool[index * 8:(index + 1) * 8]]
        bundles[str(mid)] = {"wheelhouse": tracks}
        modes.append({"mode_id": mid, "label": genre, "families": [[genre.casefold(), 1]],
            "centroid": np.frombuffer(vectors[keys[index]], dtype=np.float32), "size": 8,
            "rep_keys": [keys[index]], "space": embed.content_space_id(store)})
    store.modes.replace_modes(modes, retired_ids=[], now=1234)
    store.rec_surface.put_proposals("mode_bundles", bundles, 1234)
    monkeypatch.setattr(radio_session, "RadioCandidates", RadioCandidates)


def test_monitor_upgrades_fallback_to_distinct_prepared_streams_without_changing_playback(poc, monkeypatch):
    from yt_playlist.rec import embed

    c, remote, frames, _, pool = poc
    c.start()
    play(c, "v2")
    s, original = c.session, c.session.candidates
    tracks, heard, choices = list(s.tracks), list(s.heard), dict(s.choices)
    active = s.directions.slots[s.directions.active]
    queue = list(active["tracks"])
    prepare_radio_styles(c, pool, monkeypatch)

    def forbidden(*args, **kwargs):
        pytest.fail("Radio recovery must reuse prepared pools without loading or fitting the full model")

    monkeypatch.setattr(embed, "load_content_vectors", forbidden)
    monkeypatch.setattr(embed, "build_vectors", forbidden)
    monkeypatch.setattr(embed, "build_content_vectors", forbidden)
    monkeypatch.setattr(c.ctx.store.rec_model, "content_generation", forbidden)
    waits = iter([False, True])
    monkeypatch.setattr(s.stop, "wait", lambda _: next(waits))
    c._monitor(s)

    assert s.candidates is not original and s.candidates is s.directions.candidates
    assert len(s.candidates.modes) == 4
    assert len({b["cluster_id"] for b in c.status()["directions"]}) == 4
    assert c.status()["active"]
    assert s.tracks == tracks and s.heard == heard and s.choices == choices
    assert s.directions.slots[s.directions.active]["id"] == active["id"]
    assert s.directions.slots[s.directions.active]["tracks"] == queue
    assert c.ctx.bridge.now_playing["video_id"] == "v2"
    assert remote.ids == [t["video_id"] for t in tracks]
    assert not remote.adds and not remote.removes and len(frames) == 1
    saved = json.loads(c.ctx.store.settings.get_setting("radio_current_session"))
    assert saved["fingerprint"] == s.candidates.fingerprint


def test_pool_recovery_waits_for_an_explicit_stream_switch_to_finish(poc, monkeypatch):
    c, remote, _, _, pool = poc
    c.start()
    play(c, "v2")
    s, original = c.session, c.session.candidates
    branch = c.status()["directions"][2]
    c.select(branch["id"], c.status()["menu_id"])
    requested = s.requested_direction
    prepare_radio_styles(c, pool, monkeypatch)
    c._refresh_candidates(s)
    assert s.candidates is original and s.requested_direction is requested
    c.advance(s)
    assert s.requested_direction is None and s.replacement is None
    assert s.directions.active == 2
    selected = list(s.directions.slots[2]["tracks"])
    saved_ids = list(remote.ids)
    c._refresh_candidates(s)
    assert s.candidates is not original
    assert s.directions.active == 2 and s.directions.slots[2]["tracks"] == selected
    assert remote.ids == saved_ids
    assert c.ctx.bridge.now_playing["video_id"] == "v2"


def test_missing_pools_keep_the_menu_stable_and_request_preparation_once(poc):
    c, remote, *_ = poc
    c.start()
    play(c, "v2")
    s, original = c.session, c.session.candidates
    menu = c.status()["menu_id"]
    requested = []
    c.ctx.rec_worker = SimpleNamespace(busy=False, trigger_mode_refresh=lambda: requested.append(True))
    c._refresh_candidates(s)
    s.candidates_checked_at = 0
    c._refresh_candidates(s)
    assert requested == [True]
    assert s.candidates is original and c.status()["menu_id"] == menu
    assert remote.ids == ["v1", "v2", "v3"] and not remote.adds and not remote.removes


def test_failed_pool_upgrade_preserves_the_existing_streams_until_retry(poc, monkeypatch):
    c, remote, _, _, pool = poc
    c.start()
    play(c, "v2")
    s, candidates = c.session, c.session.candidates
    directions, menu = s.directions, c.status()["menu_id"]
    prepare_radio_styles(c, pool, monkeypatch)
    update = radio_session.RadioDirections.update

    def fail(*args, **kwargs):
        raise RuntimeError("Temporary preparation failure")

    monkeypatch.setattr(radio_session.RadioDirections, "update", fail)
    with pytest.raises(RuntimeError, match="Temporary preparation failure"):
        c._refresh_candidates(s)
    assert s.candidates is candidates and s.directions is directions
    assert c.status()["menu_id"] == menu
    assert len(c.status()["directions"]) == 4
    monkeypatch.setattr(radio_session.RadioDirections, "update", update)
    s.candidates_checked_at = 0
    c._refresh_candidates(s)
    assert s.candidates is not candidates and len(s.candidates.modes) == 4
    assert remote.ids == ["v1", "v2", "v3"] and not remote.adds and not remote.removes


def test_temporary_unrecognized_readback_retries_without_writing_over_it(poc):
    c, remote, *_ = poc
    c.start()
    play(c, "v2")
    remote.ids.append("temporary-extra")
    c.ctx.bridge.now_playing["video_id"] = "v3"
    c._browser(c.session)
    with pytest.raises(PlaylistPending):
        c.advance(c.session)
    assert c.status()["active"] and not c.session.halted
    assert not remote.adds
    remote.ids.remove("temporary-extra")
    c.advance(c.session)
    assert remote.ids == ["v1", "v2", "v3", "v4"]
    assert remote.adds == [["v4"]]


def test_metadata_enrichment_is_requested_only_after_render_and_bad_fit_is_replaced(poc, monkeypatch):
    c, *_ = poc
    requests = []
    c.ctx.enrich_worker = SimpleNamespace(prioritize=lambda keys: requests.append(list(keys)))
    c.start()
    play(c, "v1")
    assert not requests
    s = c.session
    c.seen(s.directions.menu_id)
    c.seen(s.directions.menu_id)
    assert len(requests) == 1
    assert requests[0][0] == s.tracks[0]["key"]
    old = s.directions.slots[1]["tracks"][0]
    monkeypatch.setattr(s.candidates, "refresh_metadata", lambda tracks: old.update(genre="Metal", year="2000") or True)
    monkeypatch.setattr(s.candidates, "fits", lambda track, anchor: track["video_id"] != old["video_id"])
    c._refresh_metadata(s)
    assert s.metadata_revision == 1
    c.advance(s)
    assert all(old["video_id"] not in [t["video_id"] for t in b["tracks"]] for b in s.directions.slots)
    assert s.observed_video == "v1"


def test_radio_refreshes_metadata_for_current_and_played_table_rows(poc, monkeypatch):
    from yt_playlist.rec.radio_candidates import RadioCandidates

    c, *_ = poc
    requests = []
    c.ctx.enrich_worker = SimpleNamespace(prioritize=lambda keys: requests.append(list(keys)))
    c.start()
    play(c, "v1")
    play(c, "v2")
    s = c.session
    c.seen(s.directions.menu_id)
    assert requests[0][:2] == [s.tracks[1]["key"], s.tracks[0]["key"]]
    ids = c.ctx.store.tracks.track_ids_for_videos([t["video_id"] for t in s.tracks[:2]])
    for track in s.tracks[:2]:
        tid = ids[track["video_id"]]
        c.ctx.store.tracks.set_track_enrichment(tid, "Techno", "2001")
    # Use the production catalog refresh against the fixture's deterministic sampler.
    s.candidates.store = c.ctx.store
    s.candidates.affinities = {}
    monkeypatch.setattr(s.candidates, "_energy", lambda tracks: None, raising=False)
    monkeypatch.setattr(s.candidates, "refresh_metadata",
                        lambda tracks: RadioCandidates.refresh_metadata(s.candidates, tracks))
    c._refresh_metadata(s)
    state = c.status()
    assert state["metadata_revision"] == 1
    assert all(t["genre"] == "Techno" and t["year"] == "2001" for t in state["tracks"][:2])
    assert state["position"] == 1


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
    create = executor.create_generated_playlist

    def stop_during_create(*args):
        result = create(*args)
        c.stop()
        return result

    monkeypatch.setattr(executor, "create_generated_playlist", stop_during_create)
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
    c.session.stop = ImmediateEvent()
    c._monitor(c.session)
    assert c.session.stop.is_set()
    assert not c.session.monitoring
    if disconnected:
        assert c.status()["active"]  # Keep the page attached while the extension reconnects.
        assert not c.status()["connected"]


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
    create = executor.create_generated_playlist
    monkeypatch.setattr(executor, "create_generated_playlist", lambda store, title, tracks, *args:
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

    store.settings.set_setting("last_sync_at", "1000")
    with TestClient(create_app(store, lambda: {}, background_work=False), base_url="http://127.0.0.1") as client:
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
