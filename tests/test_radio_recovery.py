"""Saved-session recovery, native queue adoption and playback continuity."""
import json
import time

import pytest
from yt_playlist.web.radio_session import PlaylistRadio
from yt_playlist.web.radio_playlist_io import PlaylistPending
from tests.radio_session_support import poc, play

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


