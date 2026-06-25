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


def test_track_title_updates_and_returns_row(store):
    iid, a = _seed_one_track(store)
    c = _client(store, lambda: {iid: FakeClient()})
    r = c.post(f"/playlist/{a}/track-title", data={"video_id": "v0", "title": "Fixed Title"})
    assert r.status_code == 200 and 'data-vid="v0"' in r.text and "Fixed Title" in r.text
    assert store.playlist_tracks_detail(a)[0]["title"] == "Fixed Title"


def test_track_artist_updates_then_resets(store):
    iid, a = _seed_one_track(store)
    c = _client(store, lambda: {iid: FakeClient()})
    c.post(f"/playlist/{a}/track-artist", data={"video_id": "v0", "artist": "Fixed Artist"})
    assert store.playlist_tracks_detail(a)[0]["artist"] == "Fixed Artist"
    r = c.post(f"/playlist/{a}/track-reset", data={"video_id": "v0", "field": "artist"})
    assert r.status_code == 200 and 'data-vid="v0"' in r.text
    assert store.playlist_tracks_detail(a)[0]["artist"] == "X"   # original from _seed_one_track


def test_track_title_no_vid_returns_toast(store):
    iid, a = _seed_one_track(store)
    c = _client(store, lambda: {iid: FakeClient()})
    r = c.post(f"/playlist/{a}/track-title", data={"title": "x"})
    assert r.status_code == 422 and 'hx-swap-oob="afterbegin:#toasts"' in r.text


def test_track_row_renders_edit_affordances(store):
    iid, a = _seed_one_track(store)
    c = _client(store, lambda: {iid: FakeClient()})
    r = c.get(f"/playlist/{a}")
    assert "startEditTitle(" in r.text and "startEditArtist(" in r.text


def test_enrich_track_events_carry_rendered_row(store, monkeypatch):
    import json as _json
    import yt_playlist.providers.musicbrainz as mb
    from tests.conftest import only_provider
    monkeypatch.setattr(mb, "enrich_full",
                        lambda title, artist: ("Rock", "1998", None) if title == "S0" else (None, None, None))
    iid, a = _seed_one_track(store)
    only_provider(store, "musicbrainz")
    c = _client(store, lambda: {iid: FakeClient()})
    jid = c.post(f"/playlist/{a}/enrich").json()["job_id"]
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
    # On the Liked Music (LM) list "remove" has no playlist item to delete. It unlikes the song.
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


def test_add_tracks_backfills_duration_for_known_trackless_time(store):
    # Regression for #26: an alternate that's already in the store with no duration (e.g. previously
    # seen via plays/history sync, which inserts duration_s=None) must get its time filled in when
    # added via "find alternate version". Otherwise the playlist row shows a blank time.
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "My Mix", 1, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "Song A", "Artist X", "Alb", 200, 1)])
    store.upsert_track("v1", "Song A (Live)", "Artist X", "Alb", None, 1)   # known, but no duration yet
    c = _client(store, lambda: {iid: FakeClient()})
    track = json.dumps({"videoId": "v1", "title": "Song A (Live)", "artist": "Artist X", "duration": 250})
    assert _refreshes(c.post(f"/playlist/{a}/add-tracks", data={"track": track}))
    row = next(t for t in store.playlist_tracks_detail(a) if t["video_id"] == "v1")
    assert row["duration"] == 250


def test_add_tracks_fetches_missing_duration_for_fresh_track(store):
    # #26: a *new* alternate whose search result carried no time (YouTube's unfiltered search often
    # omits it) gets its real duration fetched at add-time, so the playlist row isn't left blank.
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "My Mix", 1, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "Song A", "Artist X", "Alb", 200, 1)])
    fc = FakeClient(song_durations={"v1": 273})           # get_song knows v1's time
    c = _client(store, lambda: {iid: fc})
    # the posted track has duration omitted entirely (mirrors a duration-less search hit)
    track = json.dumps({"videoId": "v1", "title": "Song A (Live)", "artist": "Artist X"})
    assert _refreshes(c.post(f"/playlist/{a}/add-tracks", data={"track": track}))
    row = next(t for t in store.playlist_tracks_detail(a) if t["video_id"] == "v1")
    assert row["duration"] == 273


def test_add_tracks_preserves_album_browse(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Mix", 0, "h", 1.0)
    c = _client(store, lambda: {iid: FakeClient()})
    track = {"videoId": "v9", "title": "T", "artist": "A", "album": "The Album",
             "album_browse": "MPREb_123", "duration": 200, "thumbnail": ""}
    assert _refreshes(c.post(f"/playlist/{a}/add-tracks", data={"track": json.dumps(track)}))
    assert store.playlist_tracks_detail(a)[0]["album_browse"] == "MPREb_123"


def test_add_tracks_inserts_below_anchor(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Mix", 2, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "Song A", "X", "Alb", 200, 1),
                                  store.upsert_track("v1", "Song B", "Y", "Alb", 200, 1)])
    # the fake client must materialize the added track (with a setVideoId) so the post-add reorder can
    # find its handle and move it into place, mirroring how YouTube reports the new item.
    fc = FakeClient(tracks={"PL1": [{"videoId": "v0", "setVideoId": "sv0"},
                                    {"videoId": "v1", "setVideoId": "sv1"}]},
                    catalog={"v9": {"videoId": "v9", "setVideoId": "sv9"}})
    c = _client(store, lambda: {iid: fc})
    track = json.dumps({"videoId": "v9", "title": "Song A (Live)", "artist": "X"})
    r = c.post(f"/playlist/{a}/add-tracks", data={"track": track, "after_video_id": "v0"})
    assert _refreshes(r)
    # v9 lands directly below the anchor v0, not at the end
    assert [t["video_id"] for t in store.playlist_tracks_detail(a)] == ["v0", "v9", "v1"]
    assert fc.edited[-1] == ("PL1", {"moveItem": ("sv9", "sv1")})


def test_add_tracks_inserts_below_anchor_despite_indexing_lag(store):
    # Regression for #40. YouTube has indexing lag: a just-added track often isn't visible yet on a
    # read-back, so the best-effort YouTube reorder can't find its handle and silently gives up. The
    # store order (what the UI renders on refresh) must STILL place the new track directly below the
    # anchor, rather than leaving it appended at the end.
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Mix", 2, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "Song A", "X", "Alb", 200, 1),
                                  store.upsert_track("v1", "Song B", "Y", "Alb", 200, 1)])
    # the fake client knows only the pre-existing tracks; the freshly-added v9 has no catalog entry,
    # so add_playlist_items can't materialize it on the read-back, i.e. it stays invisible (lag).
    fc = FakeClient(tracks={"PL1": [{"videoId": "v0", "setVideoId": "sv0"},
                                    {"videoId": "v1", "setVideoId": "sv1"}]})
    c = _client(store, lambda: {iid: fc})
    track = json.dumps({"videoId": "v9", "title": "Song A (Live)", "artist": "X", "duration": 250})
    r = c.post(f"/playlist/{a}/add-tracks", data={"track": track, "after_video_id": "v0"})
    assert _refreshes(r)
    # the YouTube reorder couldn't run, yet the store still slots v9 below the anchor v0
    assert [t["video_id"] for t in store.playlist_tracks_detail(a)] == ["v0", "v9", "v1"]


def test_add_tracks_empty_returns_toast(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Mix", 0, "h", 1.0)
    c = _client(store, lambda: {iid: FakeClient()})
    r = c.post(f"/playlist/{a}/add-tracks", data={})
    assert r.status_code == 422 and 'hx-swap-oob="afterbegin:#toasts"' in r.text
    assert store.get_playlist(a).track_count == 0


# --- Last.fm API key ---

def test_lastfm_key_saved_via_form(store, monkeypatch, tmp_path):
    import yt_playlist.providers.lastfm as lf
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


def test_generated_playlist_shows_feedback_control_panel(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_track("v1", "A", "ArtA", None, None)
    store.set_track_genre(a, "Techno"); store.set_track_year(a, "1995")
    pid = store.upsert_playlist(iid, "PLG", "Gen", 1, "h", 0.0)
    store.set_playlist_tracks(pid, [a])
    store.set_playlist_group("PLG", "Generated")          # mark it generated
    c = _client(store, lambda: {iid: FakeClient()})
    html = c.get(f"/playlist/{pid}").text
    assert 'id="fb-' in html                              # the feedback control panel
    assert "More of this vibe" in html                   # simple mood (kept)
    assert "a lot" not in html.lower().split("landing")[-1][:80]   # no intensity checkbox near the buttons
    assert "Detailed feedback" in html                   # advanced disclosure
    assert "By genre" in html and "By era" in html       # facet levers (no 'By track' in the panel)
    assert "More like this" in html                      # per-track feedback moved INTO the track listing
    assert "/recs/mood" in html                          # feeds the transient model


def test_non_generated_playlist_has_no_feedback_panel(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_track("v1", "A", "ArtA", None, None)
    pid = store.upsert_playlist(iid, "PLN", "Normal", 1, "h", 0.0)
    store.set_playlist_tracks(pid, [a])
    c = _client(store, lambda: {iid: FakeClient()})
    html = c.get(f"/playlist/{pid}").text
    assert 'id="fb-' not in html                          # panel only on Generated playlists
    assert "More like this" not in html                   # ...and no per-track mood feedback either


def test_generated_playlist_unenriched_nudges_to_enrich(store):
    # A "Fresh songs" generated playlist with untagged tracks: Detailed feedback still shows, but
    # explains there are no genre/era levers until you enrich (the 531 confusion).
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_track("v1", "Fresh", "NewArtist", None, None)   # no genre, no year
    pid = store.upsert_playlist(iid, "PLF", "Fresh songs", 1, "h", 0.0)
    store.set_playlist_tracks(pid, [a])
    store.set_playlist_group("PLF", "Generated")
    c = _client(store, lambda: {iid: FakeClient()})
    html = c.get(f"/playlist/{pid}").text
    assert "Detailed feedback" in html                   # the section is shown, not hidden
    assert "enrich it" in html.lower()                   # ...with a nudge to enrich
    assert "By genre" not in html                        # no empty levers


def test_generated_playlist_shows_stored_recipe(store):
    # Even an un-tagged "Fresh songs" playlist surfaces HOW it was made (the 531 fix at the root).
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_track("v1", "Fresh", "NewArtist", None, None)   # no genre/year tags
    pid = store.upsert_playlist(iid, "PLF", "Fresh songs - June 21 2026 #1", 1, "h", 0.0)
    store.set_playlist_tracks(pid, [a])
    store.set_playlist_group("PLF", "Generated")
    store.set_recipe("PLF", {"model": "fresh", "facets": {"genres": ["house"], "eras": ["2010"]},
                             "dj": {"stickiness": 0.8}}, now=1.0)
    c = _client(store, lambda: {iid: FakeClient()})
    html = c.get(f"/playlist/{pid}").text
    assert "Made from" in html and "house" in html and "2010s" in html   # the recipe, not track tags
    assert "smooth segues" not in html                                   # stickiness chip removed; journeys govern ordering
