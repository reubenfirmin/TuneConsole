"""Contract tests for the htmx Playlists bulk actions (/playlists/copy|group|delete).

The bulk routes now do their store/YouTube work and return an empty 200 with
HX-Refresh: true (htmx then does a full page reload, parity with the old
location.reload()), instead of the old JSON payloads. Fast TestClient assertions
on the header + the store mutation.

Store-mutation coverage moved here from the JSON-based test_web.py tests
(test_playlists_group_and_delete / _copy_and_copy_merge / _delete_hides_system).
"""
from fastapi.testclient import TestClient

from yt_playlist.rec import recommend
from yt_playlist.rec.actions import CLEANUP_SURFACE
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient, _track


def _client(store, provider):
    # local base_url so state-changing POSTs pass the cross-origin guard.
    return TestClient(create_app(store, provider, now_fn=lambda: 1.0), base_url="http://127.0.0.1")


def _refreshes(r):
    return r.status_code == 200 and r.headers.get("hx-refresh") == "true"


def test_group_assigns_and_refreshes(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PLA", "Alpha", 1, "h", 1.0)
    b = store.upsert_playlist(iid, "PLB", "Beta", 1, "h", 1.0)
    c = _client(store, lambda: {iid: FakeClient()})

    r = c.post("/playlists/group", data={"ids": f"{a},{b}", "name": "Faves"})
    assert _refreshes(r)
    assert r.text == ""                                # no JSON body, htmx just reloads
    assert store.get_playlist_groups() == {"PLA": "Faves", "PLB": "Faves"}


def test_delete_removes_and_refreshes(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PLA", "Alpha", 1, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v1", "S", "X", None, None, 1)])
    fc = FakeClient(tracks={"PLA": [_track("v1", "S", "X")]})
    c = _client(store, lambda: {iid: fc})

    r = c.post("/playlists/delete", data={"ids": str(a)})
    assert _refreshes(r)
    assert store.get_playlist(a) is None and fc.deleted == ["PLA"]


def test_delete_refreshes_cached_cleanup_summary(store, monkeypatch, tmp_path):
    # Two identical playlists are a pending cleanup (exact duplicates); the home card reads a
    # CACHED count/thumbnails. Deleting one must refresh that cache so the card's pending-cleanup
    # icons involving the deleted playlist disappear (issue #73).
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PLA", "Rock", 4, "h", 1.0)
    b = store.upsert_playlist(iid, "PLB", "Rock copy", 4, "h", 1.0)
    t = [store.upsert_track(f"v{i}", f"S{i}", "X", None, None, 1) for i in range(4)]
    store.set_playlist_tracks(a, t)
    store.set_playlist_tracks(b, t)
    recommend.refresh_cleanup(store, 1.0)
    assert store.get_proposals(CLEANUP_SURFACE)["count"] == 2      # both dupes pending
    fc = FakeClient(tracks={"PLA": [_track(f"v{i}", f"S{i}", "X") for i in range(4)]})
    c = _client(store, lambda: {iid: fc})

    r = c.post("/playlists/delete", data={"ids": str(a)})
    assert _refreshes(r)
    assert store.get_playlist(a) is None
    assert store.get_proposals(CLEANUP_SURFACE)["count"] == 0      # survivor is no longer a dupe


def test_delete_hides_system_playlist_and_refreshes(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    lm = store.upsert_playlist(iid, "LM", "Liked Music", 1, "h", 1.0)   # undeletable system playlist
    store.set_playlist_tracks(lm, [store.upsert_track("v1", "S", "X", None, None, 1)])
    c = _client(store, lambda: {iid: FakeClient()})

    r = c.post("/playlists/delete", data={"ids": str(lm)})
    assert _refreshes(r)
    assert store.get_playlist(lm) is not None          # survives on YouTube
    assert "LM" in store.get_hidden_playlists()        # just hidden from the tab


def test_copy_creates_playlist_and_refreshes(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PLA", "Rock", 2, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "S0", "X", None, None, 1,
                                                     thumbnail="https://example.com/song.jpg"),
                                  store.upsert_track("v1", "S1", "X", None, None, 1)])
    fc = FakeClient(tracks={"PLA": [_track("v0", "S0", "X"), _track("v1", "S1", "X")]})
    c = _client(store, lambda: {iid: fc})

    r = c.post("/playlists/copy", data={"ids": str(a), "name": "Rock Copy"})
    assert _refreshes(r)
    copied = next(p for p in store.get_playlists() if p.title == "Rock Copy")
    assert copied.thumbnail == "https://example.com/song.jpg"


def test_copy_merge_unions_tracks_and_refreshes(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PLA", "Rock", 2, "h", 1.0)
    b = store.upsert_playlist(iid, "PLB", "Pop", 2, "h", 1.0)
    t = [store.upsert_track(f"v{i}", f"S{i}", "X", None, None, 1) for i in range(3)]
    store.set_playlist_tracks(a, [t[0], t[1]]); store.set_playlist_tracks(b, [t[1], t[2]])
    fc = FakeClient(tracks={"PLA": [_track("v0", "S0", "X"), _track("v1", "S1", "X")],
                            "PLB": [_track("v1", "S1", "X"), _track("v2", "S2", "X")]})
    c = _client(store, lambda: {iid: fc})

    r = c.post("/playlists/copy", data={"ids": f"{a},{b}", "name": "Combined"})
    assert _refreshes(r)
    combined = next(p for p in store.get_playlists() if p.title == "Combined")
    assert combined.track_count == 3                   # union of v0,v1,v2


def test_copy_into_appends_union_skipping_dupes(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    src = store.upsert_playlist(iid, "PLA", "Rock", 2, "h", 1.0)
    dst = store.upsert_playlist(iid, "PLB", "Dest", 1, "h", 1.0)
    t = [store.upsert_track(f"v{i}", f"S{i}", "X", None, None, 1) for i in range(3)]
    store.set_playlist_tracks(src, [t[0], t[1], t[2]])
    store.set_playlist_tracks(dst, [t[1]])             # v1 already in the destination
    fc = FakeClient(tracks={"PLA": [_track("v0", "S0", "X"), _track("v1", "S1", "X"), _track("v2", "S2", "X")],
                            "PLB": [_track("v1", "S1", "X")]})
    c = _client(store, lambda: {iid: fc})

    r = c.post("/playlists/copy-into", data={"ids": str(src), "target": str(dst)})
    assert _refreshes(r)
    assert store.get_playlist_track_ids(dst) == [t[1], t[0], t[2]]   # existing kept, v0/v2 appended
    assert fc.added == [("PLB", ["v0", "v2"])]                       # v1 skipped (already present)


def test_copy_into_requires_a_destination(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PLA", "Rock", 1, "h", 1.0)
    c = _client(store, lambda: {iid: FakeClient()})

    r = c.post("/playlists/copy-into", data={"ids": str(a), "target": ""})
    assert r.status_code == 422 and "destination" in r.text.lower()  # toast, not a refresh


def test_copy_into_rejects_system_target(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    src = store.upsert_playlist(iid, "PLA", "Rock", 1, "h", 1.0)
    lm = store.upsert_playlist(iid, "LM", "Liked Music", 0, "h", 1.0)   # system playlist
    store.set_playlist_tracks(src, [store.upsert_track("v0", "S0", "X", None, None, 1)])
    c = _client(store, lambda: {iid: FakeClient()})

    r = c.post("/playlists/copy-into", data={"ids": str(src), "target": str(lm)})
    assert r.status_code == 422 and "system playlist" in r.text.lower()


def test_copy_into_rejects_cross_account_target(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    a = store.upsert_identity("main", "cred", None, True)
    b = store.upsert_identity("alt", "cred2", "BA", False)             # a second account
    src = store.upsert_playlist(a, "PLA", "Rock", 1, "h", 1.0)         # source on account A
    dst = store.upsert_playlist(b, "PLB", "Dest", 0, "h", 1.0)         # target on account B
    store.set_playlist_tracks(src, [store.upsert_track("v0", "S0", "X", None, None, 1)])
    c = _client(store, lambda: {a: FakeClient(), b: FakeClient()})

    r = c.post("/playlists/copy-into", data={"ids": str(src), "target": str(dst)})
    assert r.status_code == 422 and "same account" in r.text.lower()    # cross-account add refused


def test_promote_moves_playlist_out_of_generated(store):
    iid = store.upsert_identity("main", "cred", None, True)
    pid = store.upsert_playlist(iid, "PLG", "Gen Mix", 3, "h", 0.0)
    store.set_playlist_group("PLG", "Generated")
    assert store.get_playlist_groups().get("PLG") == "Generated"
    c = _client(store, lambda: {iid: FakeClient()})
    r = c.post(f"/playlist/{pid}/promote")
    assert r.status_code == 200 and r.headers.get("hx-refresh") == "true"
    assert store.get_playlist_groups().get("PLG", "") != "Generated"   # graduated out of quarantine


def test_promote_clears_the_marker_on_youtube_too(store):
    """The quarantine marker lives in the playlist's YouTube description, because that is what your
    machines share. Promoting has to clear it there, or every other install keeps quarantining the
    playlist - and this one re-quarantines it on the next sync, since clearing a group deletes the
    row and leaves no local trace of the decision."""
    from yt_playlist.library import executor
    iid = store.upsert_identity("main", "cred", None, True)
    pid = store.upsert_playlist(iid, "PLG", "Gen Mix", 3, "h", 0.0)
    store.set_playlist_group("PLG", "Generated")
    client = FakeClient()

    c = _client(store, lambda: {iid: client})
    c.post(f"/playlist/{pid}/promote")

    assert client.edited == [("PLG", {"description": executor.PROMOTED_DESCRIPTION})]
    assert not executor.is_generated_description(executor.PROMOTED_DESCRIPTION)


def test_promote_still_works_when_youtube_refuses_the_edit(store):
    """Best-effort upstream: the local promotion is the user's decision either way."""
    class Refuses(FakeClient):
        def edit_playlist(self, playlistId, **kw):
            raise RuntimeError("nope")

    iid = store.upsert_identity("main", "cred", None, True)
    pid = store.upsert_playlist(iid, "PLG", "Gen Mix", 3, "h", 0.0)
    store.set_playlist_group("PLG", "Generated")

    c = _client(store, lambda: {iid: Refuses()})
    r = c.post(f"/playlist/{pid}/promote")

    assert r.status_code == 200
    assert store.get_playlist_groups().get("PLG", "") != "Generated"


def test_playlist_rows_use_song_artwork_when_no_cover_is_available(store):
    import json
    import re

    iid = store.upsert_identity("main", "cred", None, True)
    cover = "https://example.com/cover.jpg"
    first = "https://example.com/first.jpg"
    second = "https://example.com/second.jpg"
    no_art = store.upsert_track("none", "No art", "Artist", None, None)
    blank_art = store.upsert_track("blank", "Blank art", "Artist", None, None, thumbnail="")
    a = store.upsert_track("a", "First art", "Artist", None, None, thumbnail=first)
    b = store.upsert_track("b", "Second art", "Artist", None, None, thumbnail=second)
    playlists = {}
    for ytm, thumbnail in (("new", None), ("generated", ""), ("covered", cover), ("empty", None)):
        pid = store.upsert_playlist(iid, ytm, ytm, 0, "", 1, thumbnail=thumbnail, created_at=1)
        playlists[ytm] = pid
        if ytm != "empty":
            store.set_playlist_tracks(pid, [no_art, blank_art, a, b])
    store.set_playlist_group("generated", "Generated")
    client = _client(store, lambda: {iid: FakeClient()})

    def rows():
        response = client.get("/playlists")
        assert response.status_code == 200
        return {row["ytm"]: row for row in json.loads(
            re.search(r"playlistsTab\((\[.*?\])\)", response.text).group(1))}

    initial = rows()
    assert initial["new"]["is_new"] is True
    assert initial["new"]["thumbnail"] == first
    assert initial["generated"]["thumbnail"] == first
    assert initial["covered"]["thumbnail"] == cover
    assert initial["empty"]["thumbnail"] is None

    # Derived artwork follows the current songs, without writing a stale playlist cover.
    store.set_playlist_tracks(playlists["new"], [no_art, b, a])
    assert rows()["new"]["thumbnail"] == second
    store.set_playlist_tracks(playlists["new"], [no_art, blank_art])
    assert rows()["new"]["thumbnail"] is None
    assert store.get_playlist(playlists["new"]).thumbnail is None


def test_playlists_page_carries_generated_created_at(store):
    # The Generated card is ordered newest-first client-side (genRows in app.js sorts by `created`),
    # which relies on each playlist's actual created_at flowing into the page rows.
    import json
    import re
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_playlist(iid, "PLold", "Older Gen", 1, "h", 0.0, created_at=100.0)
    store.upsert_playlist(iid, "PLnew", "Newer Gen", 1, "h", 0.0, created_at=200.0)
    for ytm in ("PLold", "PLnew"):
        store.set_playlist_group(ytm, "Generated")
    c = _client(store, lambda: {iid: FakeClient()})
    r = c.get("/playlists")
    assert r.status_code == 200
    rows = json.loads(re.search(r"playlistsTab\((\[.*?\])\)", r.text).group(1))
    created = {row["ytm"]: row["created"] for row in rows}
    assert created["PLold"] == 100.0 and created["PLnew"] == 200.0


def test_new_playlists_use_seven_day_creation_window_not_recent_edits(store):
    import json
    import re
    now = 2_000_000
    cutoff = now - 7 * 86400
    iid = store.upsert_identity("main", "cred", None, True)
    for ytm, created_at in (("today", now), ("boundary", cutoff), ("old", cutoff - 1),
                            ("edited", cutoff - 1), ("hidden", now), ("LM", now),
                            ("unknown", None), ("future", now + 1), ("generated", now),
                            ("recipe", None), ("303", None), ("Brexit List", None)):
        store.upsert_playlist(iid, ytm, ytm, 0, "", now, created_at=created_at)
    # Neither sync, a rename nor song edits makes an existing playlist new again.
    edited = store.upsert_playlist(iid, "edited", "Renamed", 2, "new hash", now)
    store.set_playlist_title(edited, "Renamed again", now)
    store.set_playlist_track_count(edited, 3, now)
    store.hide_playlist("hidden")
    store.set_recipe("recipe", {"theme": "updated recipe"}, now)
    store.set_playlist_group("generated", "Generated")
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: now),
                   base_url="http://127.0.0.1")
    html = c.get("/playlists").text
    rows = json.loads(re.search(r"playlistsTab\((\[.*?\])\)", html).group(1))
    assert {row["ytm"] for row in rows if row["is_new"]} == {"today", "boundary"}
    assert "hidden" not in {row["ytm"] for row in rows}
    assert next(row for row in rows if row["ytm"] == "edited")["created"] == cutoff - 1


def test_playlists_page_hides_group_view_and_column_without_groups(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_playlist(iid, "PLA", "Alpha", 1, "h", 0.0)
    c = _client(store, lambda: {iid: FakeClient()})

    html = c.get("/playlists").text

    assert "All together" not in html and "By group" not in html
    assert 'class="sorth col-group"' not in html


def test_generated_only_does_not_show_main_table_group_controls(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_playlist(iid, "PLG", "Generated playlist", 1, "h", 0.0)
    store.set_playlist_group("PLG", "Generated")
    c = _client(store, lambda: {iid: FakeClient()})

    html = c.get("/playlists").text

    assert "All together" not in html and "By group" not in html
    assert 'class="sorth col-group"' not in html
    assert "Click a column heading to sort." not in html
    assert 'class="gen-grp-head"' in html and 'class="playlist-shortcut"' in html
    assert "Generated playlists" in html and "Collapse generated playlists" in html
    assert ">Activity<span" in html and 'class="pl-activity"' in html


def test_waterfall_registry_includes_all_providers():
    from yt_playlist.providers import waterfall
    # the waterfall harness can dispatch to every provider, each exposing the probe interface
    assert set(waterfall.REGISTRY) == {"musicbrainz", "lastfm", "discogs", "deezer", "acousticbrainz"}
    for mod in waterfall.REGISTRY.values():
        assert hasattr(mod, "probe") and hasattr(mod, "available")
