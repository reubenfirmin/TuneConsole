"""Session launch, browser presence and lifecycle constraints."""
import threading
import time

import pytest
from yt_playlist.library import playlist_generated
from yt_playlist.web.radio_session import PlaylistRadio
from yt_playlist.web.radio_playlist_io import PlaylistChanged, PlaylistPending
from tests.radio_session_support import poc, play

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
    c.session.stop = ImmediateEvent()
    c._monitor(c.session)
    assert c.session.stop.is_set()
    assert not c.session.monitoring
    if disconnected:
        assert c.status()["active"]  # Keep the page attached while the extension reconnects.
        assert not c.status()["connected"]
