from fastapi.testclient import TestClient
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store, provider):
    return TestClient(create_app(store, provider, now_fn=lambda: 1.0), base_url="http://127.0.0.1")


def test_track_like_toggles_liked_music(store):
    iid = store.upsert_identity("main", "cred", None, True)        # the master account
    store.upsert_playlist(iid, "LM", "Liked Music", 0, "h", 1.0)   # so local `liked` can reflect it
    pl = store.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1.0)
    t = store.upsert_track("v1", "Song", "X", None, None, 1)
    store.set_playlist_tracks(pl, [t])
    client = FakeClient()
    c = _client(store, lambda: {iid: client})

    # like -> filled heart, rate_song LIKE, song added to the local LM playlist
    r = c.post("/track/like", data={"video_id": "v1", "on": "1"})
    assert r.status_code == 200 and "like-btn on" in r.text and 'aria-pressed="true"' in r.text
    assert 'fill="currentColor"' in r.text                          # heart filled when liked
    assert client.rated == [("v1", "LIKE")]
    lm_id = next(p.id for p in store.get_playlists() if p.ytm_playlist_id == "LM")
    assert len(store.get_playlist_tracks_with_meta(lm_id)) == 1   # song added to local LM

    # unlike -> outline heart, rate_song INDIFFERENT, removed from local LM
    r = c.post("/track/like", data={"video_id": "v1", "on": ""})
    assert r.status_code == 200 and 'aria-pressed="false"' in r.text and 'fill="none"' in r.text
    assert client.rated[-1] == ("v1", "INDIFFERENT")
    assert len(store.get_playlist_tracks_with_meta(lm_id)) == 0   # removed from local LM


def test_liked_music_playlist_uses_header_heart_not_per_row(store):
    iid = store.upsert_identity("main", "cred", None, True)
    lm = store.upsert_playlist(iid, "LM", "Liked Music", 1, "h", 1.0)
    t = store.upsert_track("v1", "Song", "X", None, None, 1)
    store.set_playlist_tracks(lm, [t])
    c = _client(store, lambda: {iid: FakeClient()})
    body = c.get(f"/playlist/{lm}").text
    assert "track-table is-lm" in body                      # per-row hearts hidden on the LM playlist
    assert "lm-heart" in body and "Liked Music" in body     # a single heart in the header instead


def test_merge_with_liked_only_offers_keep_liked_or_all(store):
    iid = store.upsert_identity("main", "cred", None, True)
    lm = store.upsert_playlist(iid, "LM", "Liked Music", 1, "h", 1.0)
    pl = store.upsert_playlist(iid, "PLA", "Road Trip", 1, "h", 1.0)
    t = store.upsert_track("v1", "Song", "X", None, None, 1)
    store.set_playlist_tracks(lm, [t]); store.set_playlist_tracks(pl, [t])
    c = _client(store, lambda: {iid: FakeClient()})
    body = c.get(f"/merge?ids={lm},{pl}").text
    assert "(Road Trip), delete" not in body              # a non-Liked keeper would delete Liked -> hidden
    assert f'value="{lm}" checked' in body                # Liked is the default keeper
    assert 'value="all"' in body and "can’t be deleted" in body   # "all" still offered + the hint
    # the keep field rejects a non-Liked keeper even if posted directly
    c.post(f"/merge/update?ids={lm},{pl}", data={"field": "keep", "value": str(pl)})
    assert f'value="{lm}" checked' in c.get(f"/merge?ids={lm},{pl}").text   # unchanged: still Liked


def test_track_like_without_master_returns_toast(store):
    store.upsert_identity("alt", "cred", None, False)             # no master configured
    c = _client(store, lambda: {})
    r = c.post("/track/like", data={"video_id": "v1", "on": "1"})
    assert r.status_code == 422 and "main account" in r.text
