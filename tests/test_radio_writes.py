"""Uncertain playlist writes, readback and substitution acknowledgements."""
import json
import threading

import pytest
from yt_playlist.web import radio_session
from yt_playlist.web.radio_session import PlaylistRadio
from yt_playlist.web.radio_playlist_io import PlaylistPending
from tests.radio_session_support import poc, play

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
    c.listening.observe(c.session)
    assert "changed elsewhere" in c.status()["error"], "a healthy observer must not hide a halted playlist"
    assert remote.ids == ["v1", "v2", "v3", "manual-song"]
    assert remote.adds == []


