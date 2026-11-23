"""Uncertain playlist writes, readback and substitution acknowledgements."""
import threading

import pytest
from yt_playlist.web import radio_session
from yt_playlist.core import radio_storage
from yt_playlist.web.radio_session import PlaylistRadio
from yt_playlist.web.radio_playlist_io import PlaylistChanged, PlaylistPending
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
    c, remote, _, _, pool = poc
    pool[3].update(_radio_rotation='new', _radio_rotation_slot='new')
    pool[30].update(_radio_rotation='familiar', _radio_rotation_slot='unplayed')
    c.ctx.store.rec_ledger.record_recommendation_outcomes('prior-listen', 'start', [pool[30]['key']], 1)
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
        saved = c.persistence.load()
        assert saved["pending_result"]["videoId"] == "v31"
        restored = PlaylistRadio(c.ctx)
        restored.join()
        c = restored
    play(c, "v3")
    assert remote.adds == [["v4"]]
    assert c.status()["tracks"][-1]["video_id"] == "v31"
    assert c.status()["tracks"][-1]["key"] != original_key
    assert c.session.tracks[-1]['_radio_rotation_slot'] == 'familiar'
    assert c.session.pending is None and c.status()["ahead"] == 1
    assert "v4" in c.session.rejected
    assert c.status()["error"] is None


@pytest.mark.parametrize("replacement", [False, True], ids=["append", "direction"])
@pytest.mark.parametrize("resume", ["live", "restart", "conflict"])
def test_add_receipt_handles_video_and_artist_substitution(poc, replacement, resume):
    from yt_playlist.web.radio_playlist_tail import RadioPlaylistTail
    from yt_playlist.web.radio_state import Phase

    c, remote, _, _, pool = poc
    c.start()
    requested, accepted = pool[3], pool[30]
    requested.update(title="Ride (Vince Clarke Remix)", artist="Dubfire & Miss Kittin", duration=326)
    accepted.update(title=requested["title"], artist="Dubfire", duration=326)
    if replacement:
        c.session.edits.begin(RadioPlaylistTail(c.session.tracks, [requested], 0))
    read, add = remote.get_playlist, remote.add_playlist_items

    def read_substitution(playlist, limit=None):
        detail = read(playlist, limit)
        for row in detail["tracks"]:
            if row["videoId"] == "v31":
                row.update(title=accepted["title"], artists=[{"name": accepted["artist"]}],
                           duration_seconds=326)
        return detail

    def substitute(playlist, ids):
        if ids != ["v4"]:
            return add(playlist, ids)
        remote.adds.append(list(ids))
        remote.ids.append("v31")
        remote.fail_read = resume != "live"
        # The receipt can name the requested ID while readback exposes another
        # upload and artist credit under the same playlist occurrence handle.
        return {"status": "STATUS_SUCCEEDED", "playlistEditResults": [
            {"videoId": "v4", "setVideoId": "set-v31"}]}

    remote.get_playlist, remote.add_playlist_items = read_substitution, substitute
    if resume == "live":
        play(c, "v3")
    else:
        with pytest.raises(RuntimeError, match="Read response lost"):
            play(c, "v3")
        saved = c.persistence.load()
        receipt = saved["replacement"]["receipt"] if replacement else saved["pending_result"]
        assert receipt == {"status": "STATUS_SUCCEEDED", "videoId": "v4", "setVideoId": "set-v31"}
        if resume == "conflict":
            saved.update(phase=Phase.CONFLICT, halted=True,
                         error="The playlist was changed elsewhere. Radio stopped to preserve those changes.")
            with c.ctx.store.conn:
                radio_storage.save(c.ctx.store.conn, saved, expected=c.persistence.revision)
        c = PlaylistRadio(c.ctx)
        c.join()
        if resume == "conflict":
            assert c.session.halted
            assert c._recover(c.session)
        play(c, "v3")

    assert remote.ids == ["v1", "v2", "v3", "v31"]
    assert remote.adds == [["v4"]] and not remote.removes
    assert c.session.pending is None and c.session.replacement is None
    assert c.status()["tracks"][-1]["video_id"] == "v31"
    assert c.status()["tracks"][-1]["key"] == requested["key"]
    assert c.session.phase == Phase.PLAYING and c.status()["error"] is None
    assert "v4" in c.session.rejected
    assert c.session.directions.feedback == {}
    play(c, "v31")
    assert remote.adds == [["v4"], ["v5"]]
    assert c.status()["ahead"] == 1


@pytest.mark.parametrize('tail', ['owned', 'receipt_only', 'readback_only', 'saved_conflict', 'lost_cleanup_read',
                                 'unrelated', 'prefix_handle', 'no_saved_id', 'conflicting_saved_ids'])
def test_duplicate_playable_append_preserves_prefix_and_resumes_only_when_owned(poc, caplog, tail):
    from yt_playlist.web.radio_state import Phase

    c, remote, *_ = poc
    assert c.start()['ok']
    read, add = remote.get_playlist, remote.add_playlist_items
    handle = 'set-v2' if tail == 'prefix_handle' else 'set-new'
    receipt_handle = 'set-other' if tail == 'unrelated' else handle
    saved_id = None if tail in {'receipt_only', 'no_saved_id', 'saved_conflict', 'lost_cleanup_read'} else 'v4'
    if tail == 'conflicting_saved_ids':
        saved_id = 'v42'
    removed = []

    def readback(playlist, limit=None):
        detail = read(playlist, limit)
        rows = detail['tracks']
        if len(rows) > 3 and rows[3]['videoId'] == 'v2':
            rows[3]['setVideoId'] = handle
            if saved_id:
                rows[3]['playlistVideoId'] = saved_id
        return detail

    def append(playlist, videos):
        if videos != ['v4']:
            return add(playlist, videos)
        remote.adds.append(videos)
        remote.ids.append('v2')
        remote.fail_read = tail == 'saved_conflict'
        receipt = {'setVideoId': receipt_handle}
        if tail not in {'readback_only', 'no_saved_id'}:
            receipt['videoId'] = 'v4'
        return {'status': 'STATUS_SUCCEEDED', 'playlistEditResults': [receipt]}

    def remove(playlist, rows):
        assert playlist == 'PLSESSION' and len(rows) == 1
        assert rows[0]['videoId'] == 'v4' and rows[0]['setVideoId'] == 'set-new'
        removed.append(rows[0])
        assert remote.ids.pop() == 'v2'
        if tail == 'lost_cleanup_read':
            remote.fail_read = True
            raise RuntimeError('Cleanup response lost')
        return 'STATUS_SUCCEEDED'

    remote.get_playlist, remote.add_playlist_items, remote.remove_playlist_items = readback, append, remove
    if tail in {'unrelated', 'prefix_handle', 'no_saved_id', 'conflicting_saved_ids'}:
        with caplog.at_level('INFO'), pytest.raises(PlaylistPending):
            play(c, 'v3')
        c.session.edits.conflict_at -= 31
        with pytest.raises(PlaylistChanged):
            play(c, 'v3')
        assert remote.ids == ['v1', 'v2', 'v3', 'v2']
        assert [row['video_id'] for row in c.status()['tracks']] == remote.ids[:-1]
        assert remote.adds == [['v4']] and not removed
        assert 'playlistVideoId' in caplog.text and repr(('v2', saved_id, handle)) in caplog.text
        assert not c._recover(c.session)
        return

    if tail in {'saved_conflict', 'lost_cleanup_read'}:
        with pytest.raises(RuntimeError, match='Read response lost'):
            play(c, 'v3')
        saved = c.persistence.load()
        if tail == 'saved_conflict':
            saved.update(phase=Phase.CONFLICT, halted=True)
            with c.ctx.store.conn:
                radio_storage.save(c.ctx.store.conn, saved, expected=c.persistence.revision)
        else:
            assert saved['pending_remove'] == {'videoId': 'v4', 'setVideoId': 'set-new'}
        c = PlaylistRadio(c.ctx)
        c.join()
        if tail == 'saved_conflict':
            assert c.session.halted and c._recover(c.session)
    play(c, 'v3', position=299)
    assert remote.ids == ['v1', 'v2', 'v3', 'v5']
    assert c.ctx.bridge.native_ids == remote.ids
    assert [row['video_id'] for row in c.status()['tracks']] == remote.ids
    assert [row['video_id'] for row in c.ctx.store.charts.playlist_tracks_detail(
        c.session.local_id)] == remote.ids
    assert remote.adds == [['v4'], ['v5']] and len(removed) == 1
    assert c.session.pending is None and c.status()['ahead'] == 1
    assert c.status()['error'] is None and c.session.directions.feedback == {}
    play(c, 'v5')
    assert remote.ids[-1] == 'v6' and c.status()['ahead'] == 1


@pytest.mark.parametrize("replacement", [False, True], ids=["append", "direction"])
@pytest.mark.parametrize("matching_metadata", [False, True])
def test_an_unacknowledged_different_tail_is_not_claimed_as_our_substitution(poc, replacement, matching_metadata):
    from yt_playlist.web.radio_playlist_tail import RadioPlaylistTail

    c, remote, *_ = poc
    c.start()
    requested = c.session.candidates.by_video["v4"]
    requested["duration"] = 326
    if replacement:
        c.session.edits.begin(RadioPlaylistTail(c.session.tracks, [requested], 0))
    read = remote.get_playlist

    def metadata(playlist, limit=None):
        detail = read(playlist, limit)
        if matching_metadata:
            for row in detail["tracks"]:
                if row["videoId"] == "v31":
                    row.update(title=requested["title"], artists=[{"name": requested["artist"]}],
                               duration_seconds=326)
        return detail

    def unrelated_tail(playlist, ids):
        remote.adds.append(list(ids))
        remote.ids.append("v31")
        return {"status": "STATUS_SUCCEEDED", "playlistEditResults": [
            {"videoId": "v31", "setVideoId": "different-handle"}]}
    remote.get_playlist = metadata
    remote.add_playlist_items = unrelated_tail
    with pytest.raises((PlaylistPending, PlaylistChanged)):
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
        saved = c.persistence.load()
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
            if self.calls > 3:
                c.session.stop.set()
            return self.is_set()
    monkeypatch.setattr(radio_session.time, "monotonic", lambda: tick[0])
    c.session.wake = ImmediateEvent()
    c._monitor(c.session)
    assert c.status()["active"]
    assert c.session.halted
    assert "changed elsewhere" in c.status()["error"]
    c.listening.observe(c.session)
    assert "changed elsewhere" in c.status()["error"], "a healthy observer must not hide a halted playlist"
    assert remote.ids == ["v1", "v2", "v3", "manual-song"]
    assert remote.adds == []


