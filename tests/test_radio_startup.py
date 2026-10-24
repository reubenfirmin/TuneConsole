"""Radio startup with old playback reports and partially accepted YouTube writes."""
import time

import pytest

from tests.radio_session_support import poc, play
from yt_playlist.web.radio_playlist_io import PlaylistChanged, PlaylistPending
from yt_playlist.web.radio_session import PlaylistRadio


@pytest.mark.parametrize("presence", ["missing", "stale", "disconnected", "paused", "other_playlist"])
def test_old_checkpoint_does_not_replace_song_choices_with_a_restore_loader(poc, presence):
    c, _, frames, *_ = poc
    c.start()
    play(c, "v1")
    c.suspend()
    bridge = c.ctx.bridge
    if presence == "missing":
        bridge.now_playing = None
        bridge.now_playing_seen_at = None
    elif presence == "stale":
        bridge.now_playing_seen_at = time.monotonic() - 20
    elif presence == "disconnected":
        bridge.connected = False
    elif presence == "paused":
        bridge.now_playing["paused"] = True
    else:
        bridge.now_playing["playlist_ytm_id"] = "MIXTAPE"
    restarted = PlaylistRadio(c.ctx)
    before = len(frames)
    for _ in range(3):
        state = restarted.bootstrap()["state"]
        assert not state["active"]
        assert not state.get("resolving")
        restarted.join()
    assert len(frames) == before


@pytest.mark.parametrize("accepted", [["v5", "v2"], ["v1", "v2"], ["v5", "v1"], ["v2"]])
def test_launch_uses_a_settled_complete_subset_without_repeating_the_write(poc, monkeypatch, accepted):
    c, remote, frames, workers, _ = poc
    read = remote.get_playlist

    def saved(*args, **kwargs):
        return {**read(*args, **kwargs), "tracks": [
            {"videoId": video, "setVideoId": "set-" + video} for video in accepted],
            "trackCount": len(accepted)}

    monkeypatch.setattr(remote, "get_playlist", saved)
    assert c.start("v5")["starting"]
    s = c.session
    assert frames == []
    with pytest.raises(PlaylistPending):
        c._complete_start(s)
    s.preparation_seen_at = time.monotonic() - 10
    c._complete_start(s)
    assert [t["video_id"] for t in s.tracks] == accepted
    assert [t["video_id"] for t in c.ctx.store.charts.playlist_tracks_detail(s.local_id)] == accepted
    assert frames == [{"type": "navigate", "url":
                       f"https://music.youtube.com/watch?v={accepted[0]}&list=PLSESSION"}]
    assert len(workers) == 1
    assert not remote.adds and not remote.removes
    assert not s.initial_tracks
    assert s.directions.seed["video_id"] == accepted[0]
    assert set(s.choices) == {accepted[0]}
    assert s.rejected == {"v5", "v1", "v2"} - set(accepted)
    assert not s.rejected.intersection(t["video_id"] for b in c.status()["directions"] for t in b["tracks"])
    # The durable checkpoint contains only the rows YouTube actually saved.
    assert [t["video_id"] for t in c.persistence.load()["tracks"]] == accepted


def test_initial_partial_read_can_catch_up_before_the_settling_window(poc, monkeypatch):
    c, remote, frames, *_ = poc
    read = remote.get_playlist
    monkeypatch.setattr(remote, "get_playlist", lambda *args, **kwargs: {
        "tracks": [{"videoId": "v1"}], "trackCount": 1})
    assert c.start()["starting"] and not frames
    monkeypatch.setattr(remote, "get_playlist", read)
    c._complete_start(c.session)
    assert [t["video_id"] for t in c.session.tracks] == ["v1", "v2", "v3"]
    assert not c.session.rejected
    assert len(frames) == 1


def test_an_incomplete_read_restarts_the_initial_settling_window(poc, monkeypatch):
    c, remote, frames, *_ = poc
    detail = {"tracks": [{"videoId": "v1"}], "trackCount": 1}
    monkeypatch.setattr(remote, "get_playlist", lambda *args, **kwargs: dict(detail))
    c.start()
    c.session.preparation_seen_at = time.monotonic() - 10
    detail["continuation"] = "next"
    with pytest.raises(PlaylistPending):
        c._complete_start(c.session)
    detail.pop("continuation")
    with pytest.raises(PlaylistPending):
        c._complete_start(c.session)
    assert not frames
    c.session.preparation_seen_at = time.monotonic() - 10
    c._complete_start(c.session)
    assert len(frames) == 1


def test_an_empty_settled_playlist_fails_without_navigating_or_retrying_writes(poc, monkeypatch):
    c, remote, frames, *_ = poc
    monkeypatch.setattr(remote, "get_playlist", lambda *args, **kwargs: {"tracks": [], "trackCount": 0})
    c.start()
    c.session.preparation_seen_at = time.monotonic() - 10
    with pytest.raises(PlaylistChanged, match="did not save any playable songs"):
        c._complete_start(c.session)
    assert not frames and not remote.adds and not remote.removes


@pytest.mark.parametrize("detail", [
    {"tracks": [{"videoId": "v1"}, {"videoId": "v2"}], "trackCount": 3},
    {"tracks": [{"videoId": "v1"}, {"videoId": "v2"}]},
    {"tracks": [{"videoId": "v1"}], "trackCount": 1, "continuation": "next"},
    {"tracks": [{"videoId": "v1"}], "trackCount": 1, "_readAudit": {"exhausted": True, "omitted": 1}},
    {"tracks": [{"videoId": "v2"}, {"videoId": "v1"}], "trackCount": 2},
    {"tracks": [{"videoId": "unrelated"}], "trackCount": 1},
])
def test_settling_never_accepts_incomplete_or_unowned_initial_contents(poc, monkeypatch, detail):
    c, remote, frames, *_ = poc
    monkeypatch.setattr(remote, "get_playlist", lambda *args, **kwargs: detail)
    c.start()
    c.session.preparation_seen_at = time.monotonic() - 10
    with pytest.raises(PlaylistPending):
        c._complete_start(c.session)
    assert frames == []
    assert c.session.initial_tracks
    assert not c.session.tracks
