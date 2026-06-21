"""Contract tests for the htmx playlist-detail routes (rename / track-year / track-genre /
remove-track / reorder / alternates / add-tracks / lastfm-key).

These routes now consume form data and return _partials fragments (or HX-Refresh), instead of
the old JSON payloads. Coverage moved here from the JSON-based tests in test_web.py as each
route is converted.
"""
import json

from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient, _track


def _client(store, provider):
    return TestClient(create_app(store, provider, now_fn=lambda: 1.0), base_url="http://127.0.0.1")


def _refreshes(r):
    return r.status_code == 200 and r.headers.get("hx-refresh") == "true"


# --- rename ---

def test_rename_returns_head_fragment(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Old Name", 1, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "S", "X", "Al", 200, 1)])
    fc = FakeClient()
    c = _client(store, lambda: {iid: fc})

    r = c.post(f"/playlist/{a}/rename", data={"title": "  New Name  "})
    assert r.status_code == 200
    assert "<!doctype html>" not in r.text.lower()        # a fragment, not the whole page
    assert "New Name" in r.text and 'id="pl-title"' in r.text
    assert store.get_playlist(a).title == "New Name"       # trimmed + persisted
    assert fc.edited == [("PL1", {"title": "New Name"})]   # pushed to YouTube


def test_rename_empty_returns_toast(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Keep", 1, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "S", "X", "Al", 200, 1)])
    c = _client(store, lambda: {iid: FakeClient()})

    r = c.post(f"/playlist/{a}/rename", data={"title": "   "})
    assert r.status_code == 422
    assert r.headers.get("hx-reswap") == "none"
    assert 'hx-swap-oob="afterbegin:#toasts"' in r.text
    assert store.get_playlist(a).title == "Keep"           # unchanged


# --- track genre / year (shared track_row fragment) ---

def _seed_one_track(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "S0", "X", "Al", 200, 1)])
    return iid, a


def test_track_genre_returns_row_fragment(store):
    iid, a = _seed_one_track(store)
    c = _client(store, lambda: {iid: FakeClient()})
    r = c.post(f"/playlist/{a}/track-genre", data={"video_id": "v0", "genre": "Jazz"})
    assert r.status_code == 200
    assert "<!doctype html>" not in r.text.lower()
    assert 'class="srow"' in r.text and 'data-vid="v0"' in r.text   # the <tr> swap unit
    assert "Jazz" in r.text                                          # rendered into the genre cell
    assert {t["video_id"]: t["genre"] for t in store.playlist_tracks_detail(a)} == {"v0": "Jazz"}


def test_track_genre_no_vid_returns_toast(store):
    iid, a = _seed_one_track(store)
    c = _client(store, lambda: {iid: FakeClient()})
    r = c.post(f"/playlist/{a}/track-genre", data={"genre": "Jazz"})
    assert r.status_code == 422 and 'hx-swap-oob="afterbegin:#toasts"' in r.text


def test_track_year_returns_row_fragment(store):
    iid, a = _seed_one_track(store)
    c = _client(store, lambda: {iid: FakeClient()})
    r = c.post(f"/playlist/{a}/track-year", data={"video_id": "v0", "year": "1991"})
    assert r.status_code == 200
    assert 'data-vid="v0"' in r.text and "1991" in r.text
    assert store.playlist_tracks_detail(a)[0]["year"] == "1991"


def test_enrich_track_events_carry_rendered_row(store, monkeypatch):
    import json as _json
    import yt_playlist.musicbrainz as mb
    monkeypatch.setattr(mb, "enrich", lambda title, artist: ("Rock", "1998") if title == "S0" else (None, None))
    iid, a = _seed_one_track(store)
    c = _client(store, lambda: {iid: FakeClient()})
    jid = c.post(f"/playlist/{a}/enrich/musicbrainz").json()["job_id"]
    with c.stream("GET", f"/playlist/enrich/events/{jid}") as st:
        body = "".join(st.iter_text())
    track_evs = [_json.loads(l[6:]) for l in body.splitlines()
                 if l.startswith("data: ") and '"type": "track"' in l]
    assert track_evs and all("row_html" in e for e in track_evs)     # server-rendered cell HTML
    hit = next(e for e in track_evs if e["video_id"] == "v0")
    assert 'class="srow"' in hit["row_html"] and "Rock" in hit["row_html"] and "1998" in hit["row_html"]


# --- remove track / reorder ---

def _seed_three(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Mix", 3, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track(f"v{i}", f"S{i}", "X", "Alb", 200, 1) for i in range(3)])
    fc = FakeClient(tracks={"PL1": [{"videoId": f"v{i}", "setVideoId": f"sv{i}"} for i in range(3)]})
    return iid, a, fc


def test_remove_track_returns_empty_and_drops_row(store):
    iid, a, fc = _seed_three(store)
    c = _client(store, lambda: {iid: fc})
    r = c.post(f"/playlist/{a}/remove-track", data={"video_id": "v1"})
    assert r.status_code == 200 and r.text == ""          # empty body -> htmx swaps the row out
    assert fc.removed == [("PL1", [{"videoId": "v1", "setVideoId": "sv1"}])]
    assert [t["video_id"] for t in store.playlist_tracks_detail(a)] == ["v0", "v2"]
    assert store.get_playlist(a).track_count == 2


def test_remove_track_from_liked_music_unlikes(store):
    # On the Liked Music (LM) list "remove" has no playlist item to delete — it unlikes the song.
    iid = store.upsert_identity("main", "cred", None, True)
    lm = store.upsert_playlist(iid, "LM", "Liked Music", 1, "h", 1.0)
    store.set_playlist_tracks(lm, [store.upsert_track("v0", "S0", "X", "Alb", 200, 1)])
    fc = FakeClient(tracks={"LM": [{"videoId": "v0", "setVideoId": "sv0"}]})
    c = _client(store, lambda: {iid: fc})
    r = c.post(f"/playlist/{lm}/remove-track", data={"video_id": "v0"})
    assert r.status_code == 200 and r.text == ""           # empty body -> htmx swaps the row out
    assert fc.rated == [("v0", "INDIFFERENT")]              # unliked on YouTube
    assert fc.removed == []                                 # not a playlist-item removal
    assert store.playlist_tracks_detail(lm) == []           # dropped from local LM membership
    assert store.get_playlist(lm).track_count == 0


def test_remove_track_no_vid_returns_toast(store):
    iid, a, fc = _seed_three(store)
    c = _client(store, lambda: {iid: fc})
    r = c.post(f"/playlist/{a}/remove-track", data={})
    assert r.status_code == 422 and 'hx-swap-oob="afterbegin:#toasts"' in r.text
    assert store.get_playlist(a).track_count == 3         # nothing removed


def test_reorder_persists_and_returns_204(store):
    iid, a, fc = _seed_three(store)
    c = _client(store, lambda: {iid: fc})
    # move v2 before v0 (to the top): htmx swap="none" -> 204, order persisted server-side
    r = c.post(f"/playlist/{a}/reorder", data={"video_id": "v2", "before_video_id": "v0"})
    assert r.status_code == 204
    assert fc.edited[-1] == ("PL1", {"moveItem": ("sv2", "sv0")})
    assert [t["video_id"] for t in store.playlist_tracks_detail(a)] == ["v2", "v0", "v1"]
    # move v2 to the end (empty successor -> bare setVideoId)
    assert c.post(f"/playlist/{a}/reorder", data={"video_id": "v2", "before_video_id": ""}).status_code == 204
    assert fc.edited[-1] == ("PL1", {"moveItem": "sv2"})
    assert [t["video_id"] for t in store.playlist_tracks_detail(a)] == ["v0", "v1", "v2"]


# --- alternate versions: search (fragment) + add (form) ---

def test_alternates_renders_results_fragment(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "My Mix", 1, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "Song A", "Artist X", "Alb", 200, 1)])
    results = [
        {"videoId": "v0", "title": "Song A", "artists": [{"name": "Artist X"}], "duration_seconds": 200},
        {"videoId": "v1", "title": "Song A (Live)", "artists": [{"name": "Artist X"}], "duration": "4:10"},
        {"videoId": "v2", "title": "Song A (Remix)", "artists": [{"name": "DJ Z"}], "duration_seconds": 190},
    ]
    c = _client(store, lambda: {iid: FakeClient(search_results=results)})

    r = c.get(f"/playlist/{a}/alternates?video_id=v0")
    assert r.status_code == 200 and "<!doctype html>" not in r.text.lower()   # a fragment
    assert "Song A (Live)" in r.text and "Song A (Remix)" in r.text           # alternates rendered
    assert 'name="track"' in r.text and '"videoId": "v1"' in r.text           # checkbox carries track JSON
    assert "4:10" in r.text                                                    # duration formatted server-side


def test_add_tracks_appends_and_refreshes(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "My Mix", 1, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "Song A", "Artist X", "Alb", 200, 1)])
    fc = FakeClient()
    c = _client(store, lambda: {iid: fc})

    chosen = [json.dumps({"videoId": "v1", "title": "Song A (Live)", "artist": "Artist X", "duration": 250}),
              json.dumps({"videoId": "v2", "title": "Song A (Remix)", "artist": "DJ Z", "duration": 190})]
    r = c.post(f"/playlist/{a}/add-tracks", data={"track": chosen})
    assert _refreshes(r)
    assert [t["video_id"] for t in store.playlist_tracks_detail(a)] == ["v0", "v1", "v2"]
    assert store.get_playlist(a).track_count == 3 and fc.added == [("PL1", ["v1", "v2"])]


def test_add_tracks_preserves_album_browse(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Mix", 0, "h", 1.0)
    c = _client(store, lambda: {iid: FakeClient()})
    track = {"videoId": "v9", "title": "T", "artist": "A", "album": "The Album",
             "album_browse": "MPREb_123", "duration": 200, "thumbnail": ""}
    assert _refreshes(c.post(f"/playlist/{a}/add-tracks", data={"track": json.dumps(track)}))
    assert store.playlist_tracks_detail(a)[0]["album_browse"] == "MPREb_123"


def test_add_tracks_empty_returns_toast(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Mix", 0, "h", 1.0)
    c = _client(store, lambda: {iid: FakeClient()})
    r = c.post(f"/playlist/{a}/add-tracks", data={})
    assert r.status_code == 422 and 'hx-swap-oob="afterbegin:#toasts"' in r.text
    assert store.get_playlist(a).track_count == 0


# --- Last.fm API key ---

def test_lastfm_key_saved_via_form(store, monkeypatch, tmp_path):
    import yt_playlist.lastfm as lf
    monkeypatch.delenv("LASTFM_API_KEY", raising=False)
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "S", "X", "Al", 200, 1)])
    c = _client(store, lambda: {iid: FakeClient()})

    assert lf.api_key(store) is None
    assert "enrichPanel(%d, false," % a in c.get(f"/playlist/{a}").text
    r = c.post("/settings/lastfm-key", data={"key": " abc123 "})
    assert r.status_code == 204
    assert store.get_setting("lastfm_api_key") == "abc123" and lf.api_key(store) == "abc123"
    assert "enrichPanel(%d, true," % a in c.get(f"/playlist/{a}").text       # page now reflects configured
