"""Radio startup with old playback reports and partially accepted YouTube writes."""
import time

import pytest

from tests.radio_session_support import poc, play
from yt_playlist.web.radio_playlist_io import PlaylistChanged, PlaylistPending
from yt_playlist.web.radio_session import PlaylistRadio
from yt_playlist.library import playlist_generated


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


@pytest.mark.parametrize('proof', ['receipt', 'saved_identity'])
@pytest.mark.parametrize('replace_seed', [False, True])
def test_launch_adopts_proven_playback_upload_and_skips_an_omitted_song(poc, monkeypatch, proof, replace_seed):
    c, remote, frames, _, pool = poc
    requested = pool[0 if replace_seed else 2]
    requested.update(title='Ride (Vince Clarke Remix)', artist='Dubfire & Miss Kittin', duration=326)
    c.ctx.store.tracks.upsert_track(requested['video_id'], requested['title'], requested['artist'], '', 326)
    c.ctx.store.tracks.update_metadata(requested['video_id'],
        {'title': requested['title'], 'artist': requested['artist']}, observation={})
    create = playlist_generated.create_generated_playlist

    def create_with_receipts(*args):
        result = create(*args)
        return {**result, 'added_handles': {requested['video_id']: 'owned-occurrence'}}

    monkeypatch.setattr(playlist_generated, 'create_generated_playlist', create_with_receipts)
    substitute = {'videoId': 'v31', 'setVideoId': 'owned-occurrence',
                  'title': requested['title'], 'artists': [{'name': 'Dubfire'}], 'duration_seconds': 326}
    if proof == 'saved_identity':
        substitute['playlistVideoId'] = requested['video_id']
        monkeypatch.setattr(playlist_generated, 'create_generated_playlist', create)
    rows = [substitute, {'videoId': 'v3'}] if replace_seed else [{'videoId': 'v1'}, substitute]
    monkeypatch.setattr(remote, 'get_playlist', lambda *args, **kwargs: {'tracks': rows, 'trackCount': 2})
    assert c.start()['starting'] and not frames
    s = c.session
    s.preparation_seen_at = time.monotonic() - 10
    c._complete_start(s)
    ids = [row['videoId'] for row in rows]
    assert [t['video_id'] for t in s.tracks] == ids
    assert s.directions.seed['video_id'] == ids[0]
    assert set(s.choices) == {ids[0]}
    assert s.rejected == {requested['video_id'], 'v2'}
    assert len(frames) == 1 and f'watch?v={ids[0]}&list=' in frames[0]['url']
    assert not remote.adds and not remote.removes
    assert not s.initial_handles
    alternate = c.ctx.store.tracks.playback_alternate(s.identity, requested['video_id'])
    assert alternate['video_id'] == 'v31'
    assert alternate['key'] != requested['key']
    assert [t['video_id'] for t in c.ctx.store.charts.playlist_tracks_detail(s.local_id)] == ids
    assert [t['video_id'] for t in c.persistence.load()['tracks']] == ids
    # A later launch reuses the proven upload instead of requesting the bad ID again.
    c.stop()
    assert c.start(requested['video_id'])['ok']
    assert 'v31' in remote.ids and requested['video_id'] not in remote.ids


def test_launch_skips_unplayable_saved_rows_instead_of_navigating_to_them(poc, monkeypatch):
    c, remote, frames, *_ = poc
    rows = [{'videoId': 'v1', 'isAvailable': False}, {'videoId': 'v2'}, {'videoId': 'v3'}]
    monkeypatch.setattr(remote, 'get_playlist', lambda *args, **kwargs: {'tracks': rows, 'trackCount': 3})
    assert c.start()['starting'] and not frames
    c.session.preparation_seen_at = time.monotonic() - 10
    c._complete_start(c.session)
    assert [t['video_id'] for t in c.session.tracks] == ['v2', 'v3']
    assert 'watch?v=v2&list=' in frames[0]['url']
    assert c.session.rejected == {'v1'}
