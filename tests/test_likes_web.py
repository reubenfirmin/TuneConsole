import json
from types import SimpleNamespace

import pytest

from fastapi.testclient import TestClient
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store, provider):
    return TestClient(create_app(store, provider, now_fn=lambda: 1.0), base_url="http://127.0.0.1")


def test_track_like_toggles_liked_music(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)        # the master account
    store.playlists.upsert_playlist(iid, "LM", "Liked Music", 0, "h", 1.0)   # so local `liked` can reflect it
    pl = store.playlists.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1.0)
    t = store.tracks.upsert_track("v1", "Song", "X", None, None, 1)
    store.playlists.set_playlist_tracks(pl, [t])
    client = FakeClient()
    c = _client(store, lambda: {iid: client})

    # like -> filled heart, rate_song LIKE, song added to the local LM playlist
    r = c.post("/track/like", data={"video_id": "v1", "on": "1"})
    assert r.status_code == 200 and "like-btn on" in r.text and 'aria-pressed="true"' in r.text
    assert 'fill="currentColor"' in r.text                          # heart filled when liked
    assert client.rated == [("v1", "LIKE")]
    lm_id = next(p.id for p in store.playlists.get_playlists() if p.ytm_playlist_id == "LM")
    assert len(store.playlists.get_playlist_tracks_with_meta(lm_id)) == 1   # song added to local LM

    # unlike -> outline heart, rate_song INDIFFERENT, removed from local LM
    r = c.post("/track/like", data={"video_id": "v1", "on": ""})
    assert r.status_code == 200 and 'aria-pressed="false"' in r.text and 'fill="none"' in r.text
    assert client.rated[-1] == ("v1", "INDIFFERENT")
    assert len(store.playlists.get_playlist_tracks_with_meta(lm_id)) == 0   # removed from local LM


def test_liked_music_playlist_uses_header_heart_not_per_row(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    lm = store.playlists.upsert_playlist(iid, "LM", "Liked Music", 1, "h", 1.0)
    t = store.tracks.upsert_track("v1", "Song", "X", None, None, 1)
    store.playlists.set_playlist_tracks(lm, [t])
    c = _client(store, lambda: {iid: FakeClient()})
    body = c.get(f"/playlist/{lm}").text
    assert "track-table is-lm" in body                      # per-row hearts hidden on the LM playlist
    assert "lm-heart" in body and "Liked Music" in body     # a single heart in the header instead


def test_merge_with_liked_only_offers_keep_liked_or_all(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    lm = store.playlists.upsert_playlist(iid, "LM", "Liked Music", 1, "h", 1.0)
    pl = store.playlists.upsert_playlist(iid, "PLA", "Road Trip", 1, "h", 1.0)
    t = store.tracks.upsert_track("v1", "Song", "X", None, None, 1)
    store.playlists.set_playlist_tracks(lm, [t]); store.playlists.set_playlist_tracks(pl, [t])
    c = _client(store, lambda: {iid: FakeClient()})
    body = c.get(f"/merge?ids={lm},{pl}").text
    assert "(Road Trip), delete" not in body              # a non-Liked keeper would delete Liked -> hidden
    assert f'value="{lm}" checked' in body                # Liked is the default keeper
    assert 'value="all"' in body and "can’t be deleted" in body   # "all" still offered + the hint
    # the keep field rejects a non-Liked keeper even if posted directly
    c.post(f"/merge/update?ids={lm},{pl}", data={"field": "keep", "value": str(pl)})
    assert f'value="{lm}" checked' in c.get(f"/merge?ids={lm},{pl}").text   # unchanged: still Liked


def test_add_tracks_to_liked_music_likes_each_song(store):
    """Adding an alternate version (or 'complete this playlist' pick) while viewing Liked Music must
    *like* the song. YouTube rejects directly-added tracks on the system-managed LM playlist, so the
    add is shimmed into a like, which is the only thing that actually lands a song in Liked Music."""
    iid = store.identities.upsert_identity("main", "cred", None, True)
    lm = store.playlists.upsert_playlist(iid, "LM", "Liked Music", 0, "h", 1.0)
    client = FakeClient()
    c = _client(store, lambda: {iid: client})
    # a brand-new alternate version not yet in the local catalog
    alt = {"videoId": "v9", "title": "Alt Take", "artist": "X", "album": "", "thumbnail": ""}

    r = c.post(f"/playlist/{lm}/add-tracks", data={"track": json.dumps(alt)})

    assert r.status_code == 200                                    # success refresh, not the error toast
    assert client.rated == [("v9", "LIKE")]                        # liked on YouTube, not a doomed add
    lm_meta = store.playlists.get_playlist_tracks_with_meta(lm)
    assert [m[1] for m in lm_meta] == ["v9"]                       # now shows in Liked Music locally


def test_track_like_without_master_returns_toast(store):
    iid = store.identities.upsert_identity("alt", "cred", None, False)       # no main configured
    other_account = FakeClient()
    c = _client(store, lambda: {iid: other_account})
    r = c.post("/track/like", data={"video_id": "v1", "on": "1"})
    assert r.status_code == 422 and "Choose a main account in Setup" in r.text
    assert other_account.rated == []


@pytest.mark.parametrize("status", ["LIKE", "DISLIKE", "INDIFFERENT"])
@pytest.mark.parametrize("synced_likes", [False, True])
def test_confirmed_rating_drives_both_hearts_and_survives_stale_player(store, status, synced_likes):
    from yt_playlist.library import live_plays, player_events

    iid = store.identities.upsert_identity("main", "cred", None, True)
    if synced_likes:
        store.playlists.upsert_playlist(iid, "LM", "Liked Music", 0, "h", 1)
    pl = store.playlists.upsert_playlist(iid, "Mix", "Mix", 1, "h", 1)
    tid = store.tracks.upsert_track("v1", "Song", "Artist", None, 200, 1)
    store.playlists.set_playlist_tracks(pl, [tid])
    client = FakeClient()
    app = create_app(store, lambda: {iid: client}, now_fn=lambda: 1000, background_work=False)
    c = TestClient(app, base_url="http://127.0.0.1")
    bridge = app.state.ctx.bridge
    bridge.now_playing = {"video_id": "v1", "title": "Song", "likeStatus": ""}
    payload = {"action": "dislike" if status == "DISLIKE" else "like",
               "videoId": "v1", "likeStatus": status}
    assert c.post("/now-playing/rate", json=payload).json() == {"ok": True, "error": ""}
    assert client.rated == [("v1", status)]
    assert c.get("/bridge/status").json()["now_playing"]["likeStatus"] == status
    assert store.charts.playlist_tracks_detail(pl)[0]["liked"] is (status == "LIKE")

    # A stale native LIKE after an API unlike/dislike cannot restore the old rating or model.
    ctx = SimpleNamespace(store=store)
    live_plays.handle_play_event(ctx, {"title": "Song", "artist": "Artist", "videoId": "v1",
                                     "likeStatus": "LIKE", "brandId": ""}, 1001)
    assert store.tracks.item_rating(iid, "v1")["status"] == status
    key = store.tracks.identity_key_for_video("v1")
    assert (key in store.rec_model.recent_liked_keys()) is (status == "LIKE")
    assert (key in store.rec_model.disliked_identity_keys()) is (status == "DISLIKE")

    # A subsequent confirmed action made in YouTube Music remains authoritative.
    player_events.handle_player_event(ctx, {
        "kind": "rate", "url": "https://music.youtube.com/youtubei/v1/like/removelike",
        "brandKnown": True,
        "body": json.dumps({"target": {"videoId": "v1"}}), "brandId": "",
        "phase": "settled", "success": True, "httpStatus": 200}, 1002)
    assert c.get("/bridge/status").json()["now_playing"]["likeStatus"] == "INDIFFERENT"
    assert store.charts.playlist_tracks_detail(pl)[0]["liked"] is False
    assert key not in store.rec_model.recent_liked_keys()
    assert key not in store.rec_model.disliked_identity_keys()


def test_now_playing_rating_does_not_fall_back_to_another_account(store):
    main = store.identities.upsert_identity("main", "cred", None, True)
    other = store.identities.upsert_identity("other", "cred", "brand", False)
    remote = FakeClient()
    c = _client(store, lambda: {other: remote})
    result = c.post("/now-playing/rate", json={"action": "like", "videoId": "v1", "likeStatus": "LIKE"})
    assert result.json()["ok"] is False
    assert "not connected" in result.json()["error"]
    assert remote.rated == []
    assert store.tracks.item_rating(main, "v1") is None


def test_failed_api_rating_preserves_previous_local_rating(store, monkeypatch):
    from yt_playlist.library import ratings

    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.playlists.upsert_playlist(iid, "LM", "Liked Music", 0, "h", 1)
    store.tracks.upsert_track("v1", "Song", "Artist", None, 200, 1)
    client = FakeClient()
    c = _client(store, lambda: {iid: client})
    assert c.post("/track/like", data={"video_id": "v1", "on": "1"}).status_code == 200
    previous = store.tracks.item_rating(iid, "v1")
    monkeypatch.setattr(client, "rate_song", lambda *args: {"status": "STATUS_FAILED"})
    monkeypatch.setattr(ratings, "with_retry", lambda fn: fn())
    response = c.post("/now-playing/rate", json={"action": "like", "videoId": "v1", "likeStatus": "INDIFFERENT"})
    assert response.json()["ok"] is False
    assert store.tracks.item_rating(iid, "v1") == previous
    lm = next(p for p in store.playlists.get_playlists() if p.ytm_playlist_id == "LM")
    assert len(store.playlists.get_playlist_tracks_with_meta(lm.id)) == 1
